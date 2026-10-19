import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aggregator.dedupe import (
    RETENTION,
    WINDOW,
    SeenState,
    StateError,
    is_new,
    select_new,
)
from aggregator.sources import FetchResult, FetchStatus, Item

RUN = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)  # the daily run at 06:00 UTC
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def item(n, published=RUN - HOUR, source="feed"):
    return Item(
        source_id=source,
        topic=None,
        title=f"Story {n}",
        text="text",
        published=published,
        id=f"{source}:{n}",
    )


def ok(*items, source="feed"):
    return FetchResult(source, FetchStatus.OK, list(items))


def ids(results):
    return [i.id for r in results for i in r.items]


def daily_run(state, feed, now):
    """One daily run: select, then record (the digest is assumed to be written)."""
    fresh = select_new(feed, state, now)
    state.record(feed, fresh, now)
    return ids(fresh)


# --- the 48 hour window ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "new"),
    [
        (timedelta(0), True),
        (HOUR, True),
        (WINDOW - timedelta(seconds=1), True),
        (WINDOW, True),  # exactly 48 hours old is still inside
        (WINDOW + timedelta(seconds=1), False),
        (5 * DAY, False),
        (-HOUR, True),  # dated in the future (clock skew)
    ],
)
def test_window(age, new):
    assert is_new(item(1, published=RUN - age), SeenState(), RUN) is new


def test_undated_items_are_new_once():
    state = SeenState()
    undated = ok(item(1, published=None))
    assert daily_run(state, [undated], RUN) == ["feed:1"]
    # It stays in the feed for weeks: never included again, and never pruned while it is listed.
    for day in range(1, 60):
        assert daily_run(state, [undated], RUN + day * DAY) == []
    assert "feed:1" in state


# --- acceptance criteria -----------------------------------------------------------------------


def test_a_missed_day_loses_no_items():
    state = SeenState()
    monday = RUN
    daily_run(state, [ok(item("old", monday - 2 * HOUR))], monday)

    # Published after Monday's run; the Tuesday run never happens.
    a = item("a", monday + 3 * HOUR)
    b = item("b", monday + 20 * HOUR)
    c = item("c", monday + 30 * HOUR)  # after Tuesday's missed run
    wednesday = monday + 2 * DAY

    feed = [ok(item("old", monday - 2 * HOUR), a, b, c)]
    assert daily_run(state, feed, wednesday) == ["feed:a", "feed:b", "feed:c"]


def test_a_failed_run_records_nothing_so_its_items_are_retried():
    state = SeenState()
    feed = [ok(item(1), item(2))]
    assert ids(select_new(feed, state, RUN)) == ["feed:1", "feed:2"]
    # The summarizer fails: record() is not called. The next day the items are still new.
    assert daily_run(state, feed, RUN + DAY) == ["feed:1", "feed:2"]


def test_nothing_is_summarized_twice():
    state = SeenState()
    first = [ok(item(1), item(2))]
    assert daily_run(state, first, RUN) == ["feed:1", "feed:2"]

    # The next day the feed still lists both, plus a new one.
    second = [ok(item(1), item(2), item(3, RUN + 5 * HOUR))]
    assert daily_run(state, second, RUN + DAY) == ["feed:3"]
    # And a rerun of the same day (duplicate trigger with --force) adds nothing either.
    assert daily_run(state, second, RUN + DAY) == []


def test_nothing_is_summarized_twice_across_a_save_and_load(tmp_path):
    path = tmp_path / "state" / "seen.json"
    state = SeenState.load(path)
    daily_run(state, [ok(item(1))], RUN)
    state.save()

    reloaded = SeenState.load(path)
    assert daily_run(reloaded, [ok(item(1), item(2))], RUN + HOUR) == ["feed:2"]


def test_duplicate_entries_in_one_run_are_kept_once():
    results = select_new([ok(item(1), item(1)), ok(item(1))], SeenState(), RUN)
    assert ids(results) == ["feed:1"]


def test_same_story_id_from_different_sources_is_not_a_duplicate():
    feed = [ok(item(1, source="a"), source="a"), ok(item(1, source="b"), source="b")]
    assert ids(select_new(feed, SeenState(), RUN)) == ["a:1", "b:1"]


# --- status of the results ---------------------------------------------------------------------


def test_no_new_items_keeps_the_ok_status():
    state = SeenState()
    feed = [ok(item(1))]
    daily_run(state, feed, RUN)

    (result,) = select_new(feed, state, RUN + DAY)
    assert result.status is FetchStatus.OK
    assert result.no_new_items


def test_failed_results_pass_through_unchanged():
    failed = FetchResult.failure("broken", "timed out")
    (result,) = select_new([failed], SeenState(), RUN)
    assert result == failed


def test_results_keep_their_order():
    feed = [ok(source="a"), ok(item(1, source="b"), source="b"), ok(source="c")]
    assert [r.source_id for r in select_new(feed, SeenState(), RUN)] == ["a", "b", "c"]


# --- pruning -----------------------------------------------------------------------------------


def test_ids_are_pruned_30_days_after_they_were_last_seen():
    state = SeenState()
    daily_run(state, [ok(item(1))], RUN)

    daily_run(state, [], RUN + RETENTION)  # exactly 30 days: kept
    assert "feed:1" in state
    daily_run(state, [], RUN + RETENTION + timedelta(seconds=1))
    assert "feed:1" not in state


def test_ids_still_in_a_feed_are_refreshed_and_not_pruned():
    state = SeenState()
    feed = [ok(item(1, published=RUN - HOUR))]
    daily_run(state, feed, RUN)

    # The item stays in its feed for 40 days, long past the window and the retention period.
    for day in range(1, 41):
        assert daily_run(state, feed, RUN + day * DAY) == []
    assert state.items["feed:1"] == RUN + 40 * DAY


def test_items_outside_the_window_are_not_recorded():
    state = SeenState()
    daily_run(state, [ok(item("ancient", RUN - 10 * DAY))], RUN)
    assert len(state) == 0


def test_prune_returns_the_number_dropped():
    state = SeenState(
        items={
            "old": RUN - 31 * DAY,
            "older": RUN - 90 * DAY,
            "recent": RUN - 29 * DAY,
        }
    )
    assert state.prune(RUN) == 2
    assert list(state.items) == ["recent"]
    assert state.prune(RUN) == 0


def test_pruned_ids_count_as_unseen_again_but_are_outside_the_window():
    state = SeenState()
    old = item(1, published=RUN - HOUR)
    daily_run(state, [ok(old)], RUN)
    later = RUN + 31 * DAY
    daily_run(state, [], later)  # pruned
    assert "feed:1" not in state
    # If it reappears in a feed, its date keeps it out of the digest.
    assert daily_run(state, [ok(old)], later + HOUR) == []


# --- persistence -------------------------------------------------------------------------------


def test_missing_file_is_an_empty_state(tmp_path):
    state = SeenState.load(tmp_path / "seen.json")
    assert len(state) == 0
    assert state.path == tmp_path / "seen.json"


def test_save_writes_sorted_utc_json(tmp_path):
    berlin = datetime(2026, 10, 15, 8, 0, 0, 123456, tzinfo=UTC).astimezone(
        ZoneInfo("Europe/Berlin")
    )
    state = SeenState(items={"b:2": berlin, "a:1": RUN}, path=tmp_path / "s" / "seen.json")
    path = state.save()

    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == {
        "version": 1,
        "items": {"a:1": "2026-10-15T06:00:00Z", "b:2": "2026-10-15T08:00:00Z"},
    }
    assert text.index('"a:1"') < text.index('"b:2"')
    assert text.endswith("\n")
    assert list(path.parent.iterdir()) == [path]  # no temporary file left behind


def test_round_trip(tmp_path):
    path = tmp_path / "seen.json"
    state = SeenState(items={"x:ünïcode": RUN, "y:1": RUN - DAY}, path=path)
    state.save()
    assert SeenState.load(path).items == state.items


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"items": {}}',  # no version
        '{"version": 2, "items": {}}',
        '{"version": 1}',
        '{"version": 1, "items": []}',
        '{"version": 1, "items": {"a": "yesterday"}}',
        '{"version": 1, "items": {"a": "2026-10-15T06:00:00"}}',  # no time zone
        '{"version": 1, "items": {"a": 5}}',
    ],
)
def test_corrupt_state_is_an_error_not_a_reset(tmp_path, content):
    path = tmp_path / "seen.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError, match="seen.json"):
        SeenState.load(path)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_new(item(1), SeenState(), datetime(2026, 10, 15, 6))
