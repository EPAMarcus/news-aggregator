from datetime import UTC, date, datetime, timedelta

import pytest

from aggregator import rollup
from aggregator.config import load_config
from aggregator.pipeline import Outcome, RunRequest, StepFailed, Steps, run
from aggregator.render import Digest, FrontMatter
from aggregator.rollup import collect, load_inputs, select_inputs
from aggregator.schedule import period_label
from aggregator.store import digest_path, write_digest


def write(root, level, period):
    meta = FrontMatter(
        level=level,
        period=period,
        generated_at=datetime(2026, 10, 15, 6, tzinfo=UTC),
        model="claude-sonnet-5-5",
        inputs=["a"],
    )
    write_digest(Digest(meta=meta, body=f"{level} {period}\n"), root)


def write_dailies(root, first, last):
    day = first
    while day <= last:
        write(root, "daily", day.isoformat())
        day += timedelta(days=1)


def dailies(first, last):
    return [
        ("daily", (first + timedelta(days=n)).isoformat()) for n in range((last - first).days + 1)
    ]


def test_weekly_reads_the_dailies_of_the_week(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 18), date(2026, 10, 26))  # Sunday before to Monday after
    assert select_inputs("weekly", "2026-W43", tmp_path) == dailies(
        date(2026, 10, 19), date(2026, 10, 25)
    )


def test_weekly_across_new_year(tmp_path):
    write_dailies(tmp_path, date(2025, 12, 29), date(2026, 1, 4))
    assert select_inputs("weekly", "2026-W01", tmp_path) == dailies(
        date(2025, 12, 29), date(2026, 1, 4)
    )


def test_weekly_skips_a_missing_daily(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 19), date(2026, 10, 25))
    digest_path("daily", "2026-10-21", tmp_path).unlink()
    selected = select_inputs("weekly", "2026-W43", tmp_path)
    assert ("daily", "2026-10-21") not in selected
    assert len(selected) == 6


def test_month_ending_mid_week(tmp_path):
    # October 2026: Thursday 1st to Saturday 31st; W41 to W43 lie fully inside.
    write_dailies(tmp_path, date(2026, 9, 28), date(2026, 11, 1))
    for week in ("2026-W40", "2026-W41", "2026-W42", "2026-W43", "2026-W44"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        *dailies(date(2026, 10, 1), date(2026, 10, 4)),
        ("weekly", "2026-W41"),
        ("weekly", "2026-W42"),
        ("weekly", "2026-W43"),
        *dailies(date(2026, 10, 26), date(2026, 10, 31)),
    ]


def test_month_ending_on_sunday(tmp_path):
    # May 2026: Friday 1st to Sunday 31st; the last week W22 lies fully inside.
    write_dailies(tmp_path, date(2026, 4, 27), date(2026, 5, 31))
    for week in ("2026-W18", "2026-W19", "2026-W20", "2026-W21", "2026-W22"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-05", tmp_path) == [
        *dailies(date(2026, 5, 1), date(2026, 5, 3)),
        ("weekly", "2026-W19"),
        ("weekly", "2026-W20"),
        ("weekly", "2026-W21"),
        ("weekly", "2026-W22"),
    ]


def test_month_with_a_missing_weekly_uses_its_dailies(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 1), date(2026, 10, 31))
    for week in ("2026-W41", "2026-W43"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        *dailies(date(2026, 10, 1), date(2026, 10, 4)),
        ("weekly", "2026-W41"),
        *dailies(date(2026, 10, 12), date(2026, 10, 18)),
        ("weekly", "2026-W43"),
        *dailies(date(2026, 10, 26), date(2026, 10, 31)),
    ]


def test_month_with_missing_dailies_skips_them(tmp_path):
    for day in ("2026-10-02", "2026-10-31"):
        write(tmp_path, "daily", day)
    write(tmp_path, "weekly", "2026-W42")
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        ("daily", "2026-10-02"),
        ("weekly", "2026-W42"),
        ("daily", "2026-10-31"),
    ]


def test_month_ignores_a_weekly_of_a_partial_week(tmp_path):
    write(tmp_path, "weekly", "2026-W44")  # 26 Oct to 1 Nov
    assert select_inputs("monthly", "2026-10", tmp_path) == []


def test_yearly_reads_the_monthlies(tmp_path):
    for month in ("2025-12", "2026-01", "2026-03", "2027-01"):
        write(tmp_path, "monthly", month)
    assert select_inputs("yearly", "2026", tmp_path) == [
        ("monthly", "2026-01"),
        ("monthly", "2026-03"),
    ]


def test_daily_is_not_a_rollup(tmp_path):
    with pytest.raises(ValueError, match="not rollups"):
        select_inputs("daily", "2026-10-15", tmp_path)


def test_load_inputs_reads_the_digests(tmp_path):
    write(tmp_path, "daily", "2026-10-20")
    [loaded] = load_inputs("weekly", "2026-W43", tmp_path)
    assert (loaded.level, loaded.period) == ("daily", "2026-10-20")
    assert loaded.digest.body == "daily 2026-10-20\n"


def test_collect_fails_on_an_invalid_input(tmp_path):
    write(tmp_path, "daily", "2026-10-20")
    digest_path("daily", "2026-10-20", tmp_path).write_text("no front matter\n")
    request = RunRequest("weekly", date(2026, 10, 25))
    with pytest.raises(StepFailed, match="2026-10-20"):
        collect(request, None, tmp_path)


@pytest.fixture
def config():
    return load_config()


def test_no_inputs_ends_the_run_with_an_alert_outcome(tmp_path, config):
    def collect_step(request, cfg):
        return rollup.collect(request, cfg, tmp_path)

    outcome = run(
        RunRequest("monthly", date(2026, 10, 31)),
        config,
        steps=Steps(collect=collect_step),
        digests_dir=tmp_path,
    )
    assert outcome is Outcome.NO_INPUTS
    assert outcome.is_error
    assert not digest_path(
        "monthly", period_label("monthly", date(2026, 10, 31)), tmp_path
    ).exists()
