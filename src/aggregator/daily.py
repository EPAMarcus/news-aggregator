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

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from aggregator.config import PROJECT_ROOT, Settings
from aggregator.sources import Item
from aggregator.summarize import Document, Revision, Summarizer, Summary
from aggregator.validate import check_daily, count_bullets, truncate_bullets

DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"


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
