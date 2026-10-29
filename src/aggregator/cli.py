"""Command line: `aggregator run --level <level> [--date YYYY-MM-DD] [--force] [--dry-run]`.

Exit codes: 0 when the digest was written, printed (dry run), not due or already there; 1 for a
configuration error, an unexpected error or a due digest that could not be produced or published
(no inputs, failed step, failed release); 2 for bad arguments.

Every exit code 1 of a run that is not a dry run also raises an alert issue (`aggregator.alerts`,
spec 7) with the errors and the run log. The non-zero exit triggers GitHub's workflow-failure
email, which is the only alert if the issue cannot be created.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO

from aggregator import __version__
from aggregator.alerts import Alert, Alerter
from aggregator.config import Config, ConfigError, load_config
from aggregator.daily import DailySteps
from aggregator.pipeline import RunRequest, StepFailed, Steps, run
from aggregator.release import Releaser
from aggregator.schedule import LEVELS
from aggregator.store import DEFAULT_DIGESTS_DIR

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2  # argparse's exit code for bad arguments


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}, expected YYYY-MM-DD") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aggregator",
        description="Collect news items and produce daily, weekly, monthly and yearly digests.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    run_cmd = commands.add_parser(
        "run",
        help="produce one digest",
        description=(
            "Produce the digest of one level. Without --date this is a scheduled run for today "
            "(UTC) and does nothing if the level is not due today. With --date it is a manual "
            "run or backfill for the period containing that date, with no due check. An "
            "existing digest is never replaced unless --force is given."
        ),
    )
    run_cmd.add_argument("--level", required=True, choices=LEVELS)
    run_cmd.add_argument(
        "--date",
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help="any day of the period to produce (default: today, UTC)",
    )
    run_cmd.add_argument("--force", action="store_true", help="replace an existing digest")
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest instead of writing, committing and releasing it",
    )
    return parser


def default_steps(request: RunRequest, config: Config) -> Steps:
    """The real steps where they exist (daily), the stubs for the other levels. Only the daily
    level publishes a GitHub release; the stub digests of the other levels are not released."""
    if request.level == "daily":
        return DailySteps(config).steps(Steps(release=Releaser().release))
    return Steps()


class _Tee(io.TextIOBase):
    """Writes to several streams: stderr as usual, and a buffer for the alert's run log."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _prefixed(log: str, prefix: str) -> list[str]:
    return [line.removeprefix(prefix) for line in log.splitlines() if line.startswith(prefix)]


def _run(
    request: RunRequest,
    digests_dir: Path,
    make_steps: Callable[[RunRequest, Config], Steps],
) -> tuple[str | None, list[str]]:
    """Run once. Returns the failure (None on success) and its errors, if not in the log."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return "config-error", [f"configuration error: {exc}"]
    try:
        outcome = run(request, config, steps=make_steps(request, config), digests_dir=digests_dir)
    except Exception as exc:  # the run wrapper of spec 7: a bug must still alert
        traceback.print_exc()
        return "crash", [f"unexpected error: {type(exc).__name__}: {exc}"]
    return (outcome.value if outcome.is_error else None), []


def main(
    argv: Sequence[str] | None = None,
    *,
    today: date | None = None,
    digests_dir: Path = DEFAULT_DIGESTS_DIR,
    make_steps: Callable[[RunRequest, Config], Steps] = default_steps,
    alerter: Alerter | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK

    today = today or datetime.now(UTC).date()
    if args.date is not None and args.date > today:
        parser.error(f"--date {args.date.isoformat()} is in the future")

    request = RunRequest(
        level=args.level,
        day=args.date or today,
        scheduled=args.date is None,
        force=args.force,
        dry_run=args.dry_run,
    )
    log = io.StringIO()
    with contextlib.redirect_stderr(_Tee(sys.stderr, log)):
        failure, errors = _run(request, digests_dir, make_steps)
    if failure is None:
        return EXIT_OK
    if request.dry_run:
        return EXIT_ERROR  # a dry run publishes nothing, not even an alert

    alert = Alert(
        level=request.level,
        period=request.period,
        outcome=failure,
        errors=errors or _prefixed(log.getvalue(), "error: "),
        warnings=_prefixed(log.getvalue(), "warning: "),
        log=log.getvalue(),
    )
    try:
        (alerter or Alerter()).raise_alert(alert)
    except StepFailed as exc:
        print(
            f"error: the alert issue could not be created ({exc}); GitHub's workflow-failure "
            "email is the only alert",
            file=sys.stderr,
        )
    return EXIT_ERROR
