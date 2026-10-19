"""One run of the aggregator for one level and period (ARCHITECTURE.md section 1).

    1. guard: due today? output exists? (idempotency)
    2. collect inputs
    3. summarize
    4. write digests/<level>/... .md
    5. commit + push
    6. send email

The steps are passed in as `Steps`, so tests and dry runs can replace them. The collect and
summarize steps are stubs until the fetchers (#7), dedupe (#8) and summarizer (#9, #10) exist;
commit (#17) and email (#15) are not implemented yet and are skipped.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TextIO

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


Collect = Callable[[RunRequest, Config], list[str]]
Summarize = Callable[[RunRequest, Config, list[str]], str]
Publish = Callable[[Digest, Path], None]


def stub_collect(request: RunRequest, config: Config) -> list[str]:
    """Placeholder: the source ids for a daily run, the lower-level periods for the others."""
    if request.level == "daily":
        return [s.id for s in config.enabled_sources]
    return [f"(stub) lower-level digests of {request.period}"]


def stub_summarize(request: RunRequest, config: Config, inputs: list[str]) -> str:
    categories = "\n\n".join(f"## {c}\n- (stub)" for c in config.settings.categories)
    return f"# {request.level.capitalize()} digest {request.period}\n\n{categories}\n"


def skip_publish(digest: Digest, path: Path) -> None:
    """Commit and email are not implemented yet (#15, #17)."""


@dataclass(frozen=True)
class Steps:
    collect: Collect = stub_collect
    summarize: Summarize = stub_summarize
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
    and writes, commits and sends nothing. Status messages go to `log` (default: stderr)."""
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

    inputs = steps.collect(request, config)
    if not inputs:
        # Spec 7: an empty input set skips the digest; the alert comes with #16.
        print(f"no inputs for {level} digest {period}, skipped", file=log)
        return Outcome.NO_INPUTS

    body = steps.summarize(request, config, inputs)
    digest = Digest(
        meta=FrontMatter(
            level=level,
            period=period,
            generated_at=(now or datetime.now(UTC)).replace(microsecond=0),
            model=getattr(config.settings.models, level),
            inputs=inputs,
        ),
        body=body,
    )

    if request.dry_run:
        print(f"dry run: would write {path}", file=log)
        out.write(render_digest(digest))
        return Outcome.DRY_RUN

    write_digest(digest, digests_dir, overwrite=request.force)
    print(f"wrote {path}", file=log)
    steps.commit(digest, path)
    steps.email(digest, path)
    return Outcome.WRITTEN
