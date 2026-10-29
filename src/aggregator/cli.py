"""Command line: `aggregator run --level <level> [--date YYYY-MM-DD] [--force] [--dry-run]`.

Exit codes: 0 when the digest was written, printed (dry run), not due or already there; 1 for a
configuration error or a due digest that could not be produced or published (no inputs, failed
step, failed release), so that the workflow can alert (#16); 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from aggregator import __version__
from aggregator.config import Config, ConfigError, load_config
from aggregator.daily import DailySteps
from aggregator.pipeline import RunRequest, Steps, run
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


def main(
    argv: Sequence[str] | None = None,
    *,
    today: date | None = None,
    digests_dir: Path = DEFAULT_DIGESTS_DIR,
    make_steps: Callable[[RunRequest, Config], Steps] = default_steps,
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
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return EXIT_ERROR
    outcome = run(request, config, steps=make_steps(request, config), digests_dir=digests_dir)
    return EXIT_ERROR if outcome.is_error else EXIT_OK
