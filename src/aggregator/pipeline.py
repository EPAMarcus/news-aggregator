"""One run of the aggregator for one level and period (ARCHITECTURE.md section 1).

    1. guard: due today? output exists? (idempotency)
    2. collect inputs
    3. summarize
    4. write digests/<level>/... .md
    5. record (e.g. mark the daily items as seen), only after the write
    6. commit + push
    7. send email

The steps are passed in as `Steps`, so tests and dry runs can replace them. The daily level has
real steps (`aggregator.daily.DailySteps`); weekly, monthly and yearly still use the stubs until
their rollups exist (#21, #22, #24). Commit (#17) and email (#15) are not implemented yet and are
skipped.

A run that cannot produce its digest (no inputs, or a failed step) writes nothing and returns an
error outcome, which the CLI turns into a non-zero exit code for alerting (#16).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from aggregator.config import Config
from aggregator.render import Digest, FrontMatter, render_digest
from aggregator.schedule import Level, is_due, period_label
from aggregator.store import DEFAULT_DIGESTS_DIR, digest_exists, digest_path, write_digest


class Outcome(StrEnum):
    WRITTEN = "written"
    DRY_RUN = "dry-run"
    NOT_DUE = "not-due"
    EXISTS = "exists"
    NO_INPUTS = "no-inputs"
    FAILED = "failed"

    @property
    def is_error(self) -> bool:
        """The digest was due but not produced: the run must raise an alert (spec 7)."""
        return self in (Outcome.NO_INPUTS, Outcome.FAILED)


class StepFailed(Exception):
    """A step could not do its work (e.g. the Claude call failed). The digest is skipped."""


@dataclass(frozen=True)
class RunRequest:
    level: Level
    day: date
    # Scheduled runs check that the level is due on `day`. Manual runs and backfills pass an
    # explicit date and skip that check (schedule.is_due).
    scheduled: bool = True
    force: bool = False
    dry_run: bool = False

    @property
    def period(self) -> str:
        return period_label(self.level, self.day)


@dataclass(frozen=True)
class Collected:
    """What a digest is built from."""

    inputs: list[str]  # for the front matter: source ids, or lower-level periods
    data: Any = None  # level-specific data for the summarize and record steps


@dataclass(frozen=True)
class Body:
    """The digest body and what is known about how it was produced."""

    text: str
    model: str  # the model that wrote it, for the front matter
    notes: list[str] = field(default_factory=list)  # informational, printed to the log
    problems: list[str] = field(default_factory=list)  # to flag in the alert (#16)


Collect = Callable[[RunRequest, Config], Collected]
Summarize = Callable[[RunRequest, Config, Collected], Body]
Record = Callable[[RunRequest, Collected], None]
Publish = Callable[[Digest, Path], None]


def stub_collect(request: RunRequest, config: Config) -> Collected:
    """Placeholder for the levels without real steps: the lower-level periods."""
    if request.level == "daily":
        return Collected([s.id for s in config.enabled_sources])
    return Collected([f"(stub) lower-level digests of {request.period}"])


def stub_summarize(request: RunRequest, config: Config, collected: Collected) -> Body:
    categories = "\n\n".join(f"## {c}\n- (stub)" for c in config.settings.categories)
    text = f"# {request.level.capitalize()} digest {request.period}\n\n{categories}\n"
    return Body(text=text, model=getattr(config.settings.models, request.level))


def skip_record(request: RunRequest, collected: Collected) -> None:
    """Nothing to record."""


def skip_publish(digest: Digest, path: Path) -> None:
    """Commit and email are not implemented yet (#15, #17)."""


@dataclass(frozen=True)
class Steps:
    collect: Collect = stub_collect
    summarize: Summarize = stub_summarize
    record: Record = skip_record
    commit: Publish = skip_publish
    email: Publish = skip_publish


def run(
    request: RunRequest,
    config: Config,
    *,
    steps: Steps | None = None,
    digests_dir: Path = DEFAULT_DIGESTS_DIR,
    now: datetime | None = None,
    out: TextIO | None = None,
    log: TextIO | None = None,
) -> Outcome:
    """Produce the digest for `request`. A dry run prints the digest to `out` (default: stdout)
    and writes, records, commits and sends nothing. Status messages go to `log` (default:
    stderr)."""
    steps = steps or Steps()
    out = out or sys.stdout
    log = log or sys.stderr
    level, period = request.level, request.period
    path = digest_path(level, period, digests_dir)

    if request.scheduled and not is_due(level, request.day):
        print(f"{level} digest not due on {request.day.isoformat()}, nothing to do", file=log)
        return Outcome.NOT_DUE
    if digest_exists(level, period, digests_dir) and not request.force:
        print(f"{level} digest {period} already exists ({path}), nothing to do", file=log)
        return Outcome.EXISTS

    try:
        collected = steps.collect(request, config)
        if not collected.inputs:
            # Spec 7: an empty input set skips the digest and raises an alert (#16).
            print(f"error: no inputs for {level} digest {period}, skipped", file=log)
            return Outcome.NO_INPUTS
        body = steps.summarize(request, config, collected)
    except StepFailed as exc:
        print(f"error: {level} digest {period} failed, skipped: {exc}", file=log)
        return Outcome.FAILED

    for note in body.notes:
        print(note, file=log)
    for problem in body.problems:
        print(f"warning: {problem}", file=log)

    digest = Digest(
        meta=FrontMatter(
            level=level,
            period=period,
            generated_at=(now or datetime.now(UTC)).replace(microsecond=0),
            model=body.model,
            inputs=collected.inputs,
        ),
        body=body.text,
    )

    if request.dry_run:
        print(f"dry run: would write {path}", file=log)
        out.write(render_digest(digest))
        return Outcome.DRY_RUN

    write_digest(digest, digests_dir, overwrite=request.force)
    print(f"wrote {path}", file=log)
    steps.record(request, collected)
    steps.commit(digest, path)
    steps.email(digest, path)
    return Outcome.WRITTEN
