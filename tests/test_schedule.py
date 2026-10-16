from datetime import date

import pytest

from aggregator.schedule import (
    LEVELS,
    is_due,
    is_last_day_of_month,
    is_year_end,
    month_bounds,
    period_label,
    week_bounds,
)

MON, WED, SUN = 0, 2, 6


def due_levels(day: date) -> list[str]:
    return [level for level in LEVELS if is_due(level, day)]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 31), True),
        (date(2026, 1, 30), False),
        (date(2026, 2, 28), True),
        (date(2026, 2, 27), False),
        (date(2028, 2, 28), False),  # leap year
        (date(2028, 2, 29), True),
        (date(2026, 4, 30), True),
        (date(2026, 12, 31), True),
        (date(2026, 12, 30), False),
        (date(2026, 1, 1), False),
    ],
)
def test_last_day_of_month(day, expected):
    assert is_last_day_of_month(day) is expected


def test_year_end():
    assert is_year_end(date(2026, 12, 31))
    assert not is_year_end(date(2026, 12, 30))
    assert not is_year_end(date(2026, 1, 31))
    assert not is_year_end(date(2027, 1, 1))


def test_ordinary_day_only_has_a_daily_digest():
    day = date(2026, 10, 14)  # a Wednesday in mid-month
    assert day.weekday() == WED
    assert due_levels(day) == ["daily"]


def test_sunday_also_has_a_weekly_digest():
    day = date(2026, 10, 18)
    assert day.weekday() == SUN
    assert due_levels(day) == ["daily", "weekly"]


# --- walk-through scenarios --------------------------------------------------------------------


def test_walkthrough_31_december_on_a_wednesday():
    day = date(2025, 12, 31)
    assert day.weekday() == WED

    # The daily, monthly and yearly digests are due, the weekly one is not.
    assert due_levels(day) == ["daily", "monthly", "yearly"]
    assert period_label("monthly", day) == "2025-12"
    assert period_label("yearly", day) == "2025"

    # The week of 31 December belongs to ISO week 1 of the next year and ends on Sunday 4 January.
    assert week_bounds(day) == (date(2025, 12, 29), date(2026, 1, 4))
    assert period_label("weekly", day) == "2026-W01"
    assert is_due("weekly", date(2026, 1, 4))
    assert period_label("weekly", date(2026, 1, 4)) == "2026-W01"

    # The next day only has the daily digest.
    assert due_levels(date(2026, 1, 1)) == ["daily"]


def test_walkthrough_month_ending_mid_week():
    day = date(2026, 9, 30)
    assert day.weekday() == WED

    assert due_levels(day) == ["daily", "monthly"]
    assert period_label("monthly", day) == "2026-09"

    # The week is split by the month end; its weekly digest comes on Sunday 4 October.
    assert week_bounds(day) == (date(2026, 9, 28), date(2026, 10, 4))
    assert due_levels(date(2026, 10, 4)) == ["daily", "weekly"]
    assert period_label("weekly", date(2026, 10, 4)) == "2026-W40"


def test_walkthrough_month_ending_on_a_sunday():
    day = date(2026, 5, 31)
    assert day.weekday() == SUN

    # The weekly and the monthly digest are both due on the same day.
    assert due_levels(day) == ["daily", "weekly", "monthly"]

    # The week lies fully inside the month.
    first, last = month_bounds(day)
    monday, sunday = week_bounds(day)
    assert (monday, sunday) == (date(2026, 5, 25), day)
    assert first <= monday and sunday <= last


# --- labels and bounds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 10, 15), "2026-W42"),
        (date(2024, 12, 29), "2024-W52"),  # Sunday
        (date(2024, 12, 30), "2025-W01"),  # Monday of a week that is mostly in 2025
        (date(2026, 1, 1), "2026-W01"),
        (date(2026, 12, 31), "2026-W53"),
        (date(2027, 1, 1), "2026-W53"),  # a new calendar year, still ISO week 53 of 2026
        (date(2027, 1, 4), "2027-W01"),
    ],
)
def test_weekly_labels_use_the_iso_week_year(day, label):
    assert period_label("weekly", day) == label


def test_other_labels():
    day = date(2026, 3, 5)
    assert period_label("daily", day) == "2026-03-05"
    assert period_label("monthly", day) == "2026-03"
    assert period_label("yearly", day) == "2026"


@pytest.mark.parametrize(
    ("day", "monday", "sunday"),
    [
        (date(2026, 10, 12), date(2026, 10, 12), date(2026, 10, 18)),  # Monday
        (date(2026, 10, 15), date(2026, 10, 12), date(2026, 10, 18)),
        (date(2026, 10, 18), date(2026, 10, 12), date(2026, 10, 18)),  # Sunday
        (date(2027, 1, 1), date(2026, 12, 28), date(2027, 1, 3)),
    ],
)
def test_week_bounds(day, monday, sunday):
    assert week_bounds(day) == (monday, sunday)
    assert monday.weekday() == MON
    assert sunday.weekday() == SUN


@pytest.mark.parametrize(
    ("day", "first", "last"),
    [
        (date(2026, 10, 15), date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 2, 10), date(2026, 2, 1), date(2026, 2, 28)),
        (date(2028, 2, 10), date(2028, 2, 1), date(2028, 2, 29)),
        (date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_month_bounds(day, first, last):
    assert month_bounds(day) == (first, last)
