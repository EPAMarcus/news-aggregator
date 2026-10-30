"""Input selection for the weekly, monthly and yearly digests (spec 4.1, 4.2).

    weekly   the dailies of the week, Monday to Sunday
    monthly  every existing weekly whose week lies fully inside the month, plus the dailies of
             all other days (edge days of partial weeks, days of weeks without a weekly)
    yearly   the monthlies of the year

Missing files are skipped. An empty selection makes the run end with `Outcome.NO_INPUTS`, which
raises an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from aggregator.config import Config
from aggregator.pipeline import Collected, RunRequest, StepFailed
from aggregator.render import Digest, DigestFormatError
from aggregator.schedule import Level, month_bounds, period_label, period_start, week_bounds
from aggregator.store import DEFAULT_DIGESTS_DIR, digest_exists, read_digest


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
