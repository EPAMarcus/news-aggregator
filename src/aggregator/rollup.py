"""The weekly, monthly and yearly digests: input selection (spec 4.1, 4.2) and summary.

    weekly   the dailies of the week, Monday to Sunday
    monthly  every existing weekly whose week lies fully inside the month, plus the dailies of
             all other days (edge days of partial weeks, days of weeks without a weekly)
    yearly   the monthlies of the year

Missing files are skipped. An empty selection makes the run end with `Outcome.NO_INPUTS`, which
raises an alert.

The summary works like the daily one (`aggregator.daily`): the level prompt (prompts/<level>.md)
with only the input digests as documents, word-count and structure validation, and one retry that
names the violations. A digest still too long after the retry is kept and flagged; the final
over-length policy is decided in #26. Daily inputs are passed without their Sources section and
article links (`render.without_sources`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from aggregator.config import Config, Settings
from aggregator.daily import PromptError, load_prompt
from aggregator.pipeline import Body, Collected, RunRequest, StepFailed, Steps
from aggregator.render import Digest, DigestFormatError, without_sources
from aggregator.schedule import Level, month_bounds, period_label, period_start, week_bounds
from aggregator.store import DEFAULT_DIGESTS_DIR, digest_exists, read_digest
from aggregator.summarize import Document, Revision, Summarizer, SummarizerError, Summary
from aggregator.validate import check_rollup, count_words

# How far above `limits.<level>_words` a digest may go: weekly is "at most 300 words", the
# others are "about" their length (spec 5).
WORD_TOLERANCE: Mapping[Level, float] = {"weekly": 0.0, "monthly": 0.1, "yearly": 0.1}


@dataclass(frozen=True)
class RollupInput:
    level: Level
    period: str
    digest: Digest


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _existing_dailies(days: list[date], root: Path) -> list[tuple[Level, str]]:
    labels = [period_label("daily", d) for d in days]
    return [("daily", p) for p in labels if digest_exists("daily", p, root)]


def select_inputs(
    level: Level, period: str, root: Path | str = DEFAULT_DIGESTS_DIR
) -> list[tuple[Level, str]]:
    """The existing lower-level digests that the digest of `period` is built from, in
    chronological order. Raises `ValueError` for the daily level or a malformed period."""
    root = Path(root)
    start = period_start(level, period)
    match level:
        case "weekly":
            return _existing_dailies(_days(*week_bounds(start)), root)
        case "monthly":
            first, last = month_bounds(start)
            selected: list[tuple[Level, str]] = []
            day = first
            while day <= last:
                monday, sunday = week_bounds(day)
                week = period_label("weekly", day)
                if monday >= first and sunday <= last and digest_exists("weekly", week, root):
                    selected.append(("weekly", week))
                else:
                    selected += _existing_dailies(_days(day, min(sunday, last)), root)
                day = sunday + timedelta(days=1)
            return selected
        case "yearly":
            months = [f"{start.year}-{m:02d}" for m in range(1, 13)]
            return [("monthly", p) for p in months if digest_exists("monthly", p, root)]
        case _:
            raise ValueError(f"{level} digests are not rollups")


def load_inputs(
    level: Level, period: str, root: Path | str = DEFAULT_DIGESTS_DIR
) -> list[RollupInput]:
    """The selected input digests, read and parsed."""
    return [
        RollupInput(lvl, p, read_digest(lvl, p, root))
        for lvl, p in select_inputs(level, period, root)
    ]


def collect(
    request: RunRequest, config: Config, root: Path | str = DEFAULT_DIGESTS_DIR
) -> Collected:
    """The collect step of the rollup levels: `inputs` are the input periods, `data` the
    `RollupInput`s. An unreadable or invalid input digest fails the step."""
    try:
        inputs = load_inputs(request.level, request.period, root)
    except (OSError, DigestFormatError) as exc:
        raise StepFailed(
            f"cannot read the inputs of {request.level} {request.period}: {exc}"
        ) from exc
    return Collected(inputs=[i.period for i in inputs], data=inputs)


# --- summary ------------------------------------------------------------------------------------


def word_limits(level: Level, settings: Settings) -> tuple[int, int]:
    """The target and the maximum word count of a rollup digest."""
    target: int = getattr(settings.limits, f"{level}_words")
    return target, round(target * (1 + WORD_TOLERANCE[level]))


def rollup_task(level: Level, settings: Settings, prompts_dir: Path | None = None) -> str:
    target, maximum = word_limits(level, settings)
    values = {
        "categories": ", ".join(settings.categories),
        "target_words": target,
        "max_words": maximum,
    }
    return load_prompt(level, values, prompts_dir)


def rollup_document(rollup_input: RollupInput) -> Document:
    body = rollup_input.digest.body
    if rollup_input.level == "daily":
        body = without_sources(body)
    return Document.from_digest(rollup_input.level, rollup_input.period, body)


@dataclass(frozen=True)
class RollupBody:
    text: str
    model: str
    attempts: int  # Claude answers requested: 1, or 2 after a retry
    words: int
    retried_for: list[str] = field(default_factory=list)  # problems of the first answer
    problems: list[str] = field(default_factory=list)  # problems left in the final answer
    input_tokens: int = 0
    output_tokens: int = 0


def summarize_rollup(
    level: Level,
    inputs: Sequence[RollupInput],
    settings: Settings,
    summarizer: Summarizer,
    prompts_dir: Path | None = None,
) -> RollupBody:
    categories = settings.categories
    _, max_words = word_limits(level, settings)
    task = rollup_task(level, settings, prompts_dir)
    docs = [rollup_document(i) for i in inputs]

    answers: list[Summary] = [summarizer.summarize(level, task, docs)]
    first_problems = check_rollup(answers[0].text, categories, max_words)
    problems = first_problems
    if first_problems:
        revision = Revision(previous=answers[0].text, problems=first_problems)
        answers.append(summarizer.summarize(level, task, docs, revision=revision))
        problems = check_rollup(answers[-1].text, categories, max_words)

    final = answers[-1]
    return RollupBody(
        text=final.text,
        model=final.model,
        attempts=len(answers),
        words=count_words(final.text),
        retried_for=list(first_problems) if len(answers) > 1 else [],
        problems=problems,
        input_tokens=sum(a.input_tokens for a in answers),
        output_tokens=sum(a.output_tokens for a in answers),
    )


class RollupSteps:
    """The real collect and summarize steps of the weekly and monthly levels. There is nothing to
    record. The digests directory, the summarizer and the prompts can be replaced for tests."""

    def __init__(
        self,
        config: Config,
        *,
        summarizer: Summarizer | None = None,
        digests_dir: Path = DEFAULT_DIGESTS_DIR,
        prompts_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.summarizer = summarizer or Summarizer(config.settings)
        self.digests_dir = digests_dir
        self.prompts_dir = prompts_dir

    def steps(self, base: Steps | None = None) -> Steps:
        """`base` with collect and summarize replaced (record, commit and release are kept)."""
        base = base or Steps()
        return Steps(
            collect=self.collect,
            summarize=self.summarize,
            record=base.record,
            commit=base.commit,
            release=base.release,
        )

    def collect(self, request: RunRequest, config: Config) -> Collected:
        return collect(request, config, self.digests_dir)

    def summarize(self, request: RunRequest, config: Config, collected: Collected) -> Body:
        try:
            rollup = summarize_rollup(
                request.level, collected.data, config.settings, self.summarizer, self.prompts_dir
            )
        except (SummarizerError, PromptError) as exc:
            raise StepFailed(str(exc)) from exc
        notes = [
            f"inputs: {', '.join(collected.inputs)}",
            f"claude: {rollup.model}, {rollup.attempts} answer(s), {rollup.words} words, "
            f"{rollup.input_tokens} input / {rollup.output_tokens} output tokens",
        ]
        notes += [f"retried for: {p}" for p in rollup.retried_for]
        return Body(text=rollup.text, model=rollup.model, notes=notes, problems=rollup.problems)
