"""The daily digest body: prompt, Claude call, validation and one retry (architecture 3.4).

    task = daily_task(settings)                       # prompts/daily.md, filled in
    first = summarizer.summarize("daily", task, docs)
    problems = check_daily(first.text, ...)
    if problems:                                      # one retry that names the violations
        second = summarizer.summarize("daily", task, docs, revision=Revision(first.text, problems))
        problems = check_daily(second.text, ...)
    if still over the bullet limit:                   # over-length policy, see #26
        truncate to the limit and report it

The result always carries the remaining problems, so that the run can flag them in the alert (#16)
instead of failing the digest over a formatting slip. An API failure raises SummarizerError.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import TextIO

from aggregator.config import PROJECT_ROOT, Config, Settings, Source
from aggregator.dedupe import DEFAULT_STATE_PATH, SeenState, StateError, select_new
from aggregator.pipeline import Body, Collected, RunRequest, StepFailed, Steps
from aggregator.render import contributing_sources, render_sources_section, with_sources_section
from aggregator.sources import FetchResult, Item, fetch_all
from aggregator.sources.base import Fetcher
from aggregator.summarize import Document, Revision, Summarizer, SummarizerError, Summary
from aggregator.validate import check_daily, count_bullets, truncate_bullets

DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"

# Source types the daily run fetches. Newsletter (#12) and web (#13) sources are skipped until
# their fetchers exist; add the type here when it does.
DAILY_SOURCE_TYPES = frozenset({"rss"})


class PromptError(Exception):
    """A prompt file is missing, unreadable or uses an unknown placeholder."""


def load_prompt(name: str, values: dict[str, object], prompts_dir: Path | None = None) -> str:
    """Read prompts/<name>.md and fill in its `$placeholders`."""
    path = (prompts_dir or DEFAULT_PROMPTS_DIR) / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"{path}: cannot read file ({exc})") from None
    try:
        return Template(text).substitute(values)
    except (KeyError, ValueError) as exc:
        raise PromptError(f"{path}: bad placeholder ({exc})") from None


def daily_task(settings: Settings, prompts_dir: Path | None = None) -> str:
    values = {
        "categories": ", ".join(settings.categories),
        "max_bullets": settings.limits.daily_bullets,
    }
    return load_prompt("daily", values, prompts_dir)


@dataclass(frozen=True)
class DailyBody:
    text: str  # the digest body, without the Sources section (#11)
    model: str
    attempts: int  # Claude answers requested: 1, or 2 after a retry
    retried_for: list[str] = field(default_factory=list)  # problems of the first answer
    problems: list[str] = field(default_factory=list)  # problems left in the final answer
    truncated_from: int | None = None  # bullet count before truncation, if truncated
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        """Valid without truncation. False means the run should flag it in the alert."""
        return not self.problems and self.truncated_from is None


def summarize_daily(
    items: Sequence[Item],
    settings: Settings,
    summarizer: Summarizer,
    prompts_dir: Path | None = None,
) -> DailyBody:
    categories = settings.categories
    limit = settings.limits.daily_bullets
    task = daily_task(settings, prompts_dir)
    docs = [Document.from_item(item) for item in items]

    answers: list[Summary] = [summarizer.summarize("daily", task, docs)]
    first_problems = check_daily(answers[0].text, categories, limit)
    problems = first_problems
    if first_problems:
        revision = Revision(previous=answers[0].text, problems=first_problems)
        answers.append(summarizer.summarize("daily", task, docs, revision=revision))
        problems = check_daily(answers[-1].text, categories, limit)

    final = answers[-1]
    text = final.text
    truncated_from = None
    if (count := count_bullets(text)) > limit:
        # Draft over-length policy for the daily level (spec 9, item 2; decided in #26).
        text = truncate_bullets(text, limit)
        truncated_from = count
        problems = check_daily(text, categories, limit)

    return DailyBody(
        text=text,
        model=final.model,
        attempts=len(answers),
        retried_for=list(first_problems) if len(answers) > 1 else [],
        problems=problems,
        truncated_from=truncated_from,
        input_tokens=sum(a.input_tokens for a in answers),
        output_tokens=sum(a.output_tokens for a in answers),
    )


# --- the daily run steps (pipeline.Steps) ------------------------------------------------------


@dataclass(frozen=True)
class DailyInputs:
    """What collect hands to summarize and record."""

    sources: list[Source]  # the sources that were fetched
    fetched: list[FetchResult]  # every fetch result, for refreshing the seen-state
    fresh: list[FetchResult]  # the same results with only the new items
    state: SeenState
    now: datetime

    @property
    def items(self) -> list[Item]:
        return [item for result in self.fresh for item in result.items]


class DailySteps:
    """The real collect, summarize and record steps of the daily level:

        collect    fetch the enabled RSS sources, keep the new items (dedupe, 48h window)
        summarize  Claude with the daily prompt, validation and one retry, then the Sources section
        record     mark the included items as seen; only after the digest was written, never in a
                   dry run (the pipeline does not call it then)

    Everything external can be replaced for tests: the fetchers, the summarizer, the state file
    and the clock.
    """

    def __init__(
        self,
        config: Config,
        *,
        summarizer: Summarizer | None = None,
        fetchers: Mapping[str, Fetcher] | None = None,
        state_path: Path = DEFAULT_STATE_PATH,
        prompts_dir: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        log: TextIO | None = None,
    ) -> None:
        self.config = config
        self.summarizer = summarizer or Summarizer(config.settings)
        self.fetchers = fetchers
        self.state_path = state_path
        self.prompts_dir = prompts_dir
        self.clock = clock
        self.log = log or sys.stderr

    def steps(self, base: Steps | None = None) -> Steps:
        """`base` with collect, summarize and record replaced (commit and email are kept)."""
        base = base or Steps()
        return Steps(
            collect=self.collect,
            summarize=self.summarize,
            record=self.record,
            commit=base.commit,
            email=base.email,
        )

    def _say(self, message: str) -> None:
        print(message, file=self.log)

    # --- collect -------------------------------------------------------------------------------

    def collect(self, request: RunRequest, config: Config) -> Collected:
        # The 48h window is relative to the time of the run, not to --date: feeds only hold
        # recent items, so a backfill of an old day cannot reconstruct that day anyway.
        now = self.clock()
        try:
            state = SeenState.load(self.state_path)
        except StateError as exc:
            raise StepFailed(str(exc)) from None

        sources = [s for s in config.enabled_sources if s.type in DAILY_SOURCE_TYPES]
        skipped = [s.id for s in config.enabled_sources if s.type not in DAILY_SOURCE_TYPES]
        if skipped:
            self._say(f"skipped sources without a fetcher yet: {', '.join(skipped)}")

        fetched = fetch_all(sources, self.fetchers)
        fresh = select_new(fetched, state, now)
        for result in fetched:
            if not result.ok:
                self._say(f"warning: source {result.source_id} failed: {result.error}")
        for result in fresh:
            if result.ok:
                self._say(f"source {result.source_id}: {len(result.items)} new items")

        inputs = DailyInputs(sources, fetched, fresh, state, now)
        contributors = contributing_sources(sources, fresh)
        return Collected(inputs=[s.id for s in contributors], data=inputs)

    # --- summarize -----------------------------------------------------------------------------

    def summarize(self, request: RunRequest, config: Config, collected: Collected) -> Body:
        inputs: DailyInputs = collected.data
        try:
            daily = summarize_daily(
                inputs.items, config.settings, self.summarizer, self.prompts_dir
            )
        except (SummarizerError, PromptError) as exc:
            raise StepFailed(str(exc)) from exc

        section = render_sources_section(contributing_sources(inputs.sources, inputs.fresh))
        notes = [
            f"claude: {daily.model}, {daily.attempts} answer(s), "
            f"{daily.input_tokens} input / {daily.output_tokens} output tokens"
        ]
        notes += [f"retried for: {p}" for p in daily.retried_for]
        problems = list(daily.problems)
        if daily.truncated_from is not None:
            problems.insert(
                0,
                f"digest still had {daily.truncated_from} bullets after the retry, truncated to "
                f"{config.settings.limits.daily_bullets}",
            )
        return Body(
            text=with_sources_section(daily.text, section),
            model=daily.model,
            notes=notes,
            problems=problems,
        )

    # --- record --------------------------------------------------------------------------------

    def record(self, request: RunRequest, collected: Collected) -> None:
        inputs: DailyInputs = collected.data
        inputs.state.record(inputs.fetched, inputs.fresh, inputs.now)
        path = inputs.state.save(self.state_path)
        self._say(f"updated {path}")
