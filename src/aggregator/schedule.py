"""Period math and run guards.

Workflows are scheduled in UTC (one cron entry each, no daylight-saving handling), and every date
here is a UTC date: a digest is labelled with the UTC date its run starts on. The cron times are
chosen so that this is also the owner's local date, and a late start of up to an hour does not
change it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, get_args

Level = Literal["daily", "weekly", "monthly", "yearly"]
LEVELS: tuple[Level, ...] = get_args(Level)

SUNDAY = 6  # date.weekday()


def is_last_day_of_month(day: date) -> bool:
    """True if tomorrow is the 1st ("last day of month" is not expressible in cron)."""
    return (day + timedelta(days=1)).day == 1


def is_year_end(day: date) -> bool:
    return day.month == 12 and day.day == 31


def is_due(level: Level, day: date) -> bool:
    """Whether a scheduled run on `day` must produce a digest of `level`.

    Cron fires the weekly workflow only on Sundays and the monthly one on days 28 to 31, so this
    is the guard that keeps the monthly run to the last day of the month. The yearly job runs
    after the monthly one on 31 December. Manual runs and backfills skip this check and pass an
    explicit date.
    """
    match level:
        case "daily":
            return True
        case "weekly":
            return day.weekday() == SUNDAY
        case "monthly":
            return is_last_day_of_month(day)
        case "yearly":
            return is_year_end(day)


def week_bounds(day: date) -> tuple[date, date]:
    """Monday and Sunday of the ISO week containing `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    """First and last day of the month containing `day`."""
    first = day.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last


def period_label(level: Level, day: date) -> str:
    """Label used in file names and front matter for the period `day` belongs to.

    Weeks use the ISO week-numbering year, which differs from the calendar year around New Year:
    2025-12-31 is in `2026-W01`, 2027-01-01 is in `2026-W53`.
    """
    match level:
        case "daily":
            return day.isoformat()
        case "weekly":
            iso = day.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        case "monthly":
            return f"{day.year}-{day.month:02d}"
        case "yearly":
            return f"{day.year}"
