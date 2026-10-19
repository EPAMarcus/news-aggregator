"""Seen-state handling and the 48 hour window (architecture 3.3).

`state/seen.json` maps item IDs to the last time a run saw them in a source:

    {
      "version": 1,
      "items": {
        "hacker-news:https://news.ycombinator.com/item?id=1": "2026-10-19T06:03:12Z",
        ...
      }
    }

A run includes an item if it is **unseen** and **not older than 48 hours**. Because the window is
twice the daily interval, a missed or failed day loses nothing: the next run still picks up the
previous day's items, and the seen-state keeps them from appearing twice.

Usage in a run:

    state = SeenState.load()
    fresh = select_new(results, state, now)      # what goes into the digest
    ... summarize and write the digest ...
    state.record(results, fresh, now)            # only after the digest was written
    state.save()

Recording only after success means a failed run marks nothing as seen, so its items are retried.
A dry run never saves.

IDs are pruned 30 days after a run last saw them. Every run refreshes the timestamp of IDs that are
still in a feed, so an item without a date that stays in its feed for months is never pruned and
included again. Dated items are older than the 48 hour window long before they could be pruned.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aggregator.config import PROJECT_ROOT
from aggregator.sources import FetchResult, Item

DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "seen.json"
WINDOW = timedelta(hours=48)
RETENTION = timedelta(days=30)
VERSION = 1


class StateError(Exception):
    """`seen.json` exists but cannot be read. Never silently reset: an empty state would
    summarize the last 48 hours a second time."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware, got {value!r}")
    return value


@dataclass
class SeenState:
    items: dict[str, datetime] = field(default_factory=dict)
    path: Path = DEFAULT_STATE_PATH

    # --- persistence ---------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str = DEFAULT_STATE_PATH) -> SeenState:
        """Load the state. A missing file is an empty state (the very first run)."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path=path)
        except OSError as exc:
            raise StateError(f"{path}: cannot read file ({exc})") from None
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("version") != VERSION:
                raise ValueError(f'expected an object with "version": {VERSION}')
            raw = data["items"]
            if not isinstance(raw, dict):
                raise ValueError('"items" must be an object')
            items = {str(k): _aware(datetime.fromisoformat(v)) for k, v in raw.items()}
        except (ValueError, KeyError, TypeError) as exc:
            raise StateError(f"{path}: invalid seen-state ({exc})") from None
        return cls(items=items, path=path)

    def save(self, path: Path | str | None = None) -> Path:
        """Write the state atomically, with sorted keys so that git diffs stay small."""
        path = Path(path or self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": VERSION,
            "items": {k: _timestamp(self.items[k]) for k in sorted(self.items)},
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    # --- queries and updates -------------------------------------------------------------------

    def __contains__(self, item_id: str) -> bool:
        return item_id in self.items

    def __len__(self) -> int:
        return len(self.items)

    def record(
        self, fetched: Iterable[FetchResult], included: Iterable[FetchResult], now: datetime
    ) -> None:
        """After a successful run: mark the included items as seen, refresh the timestamp of
        known IDs that are still in a feed, and prune IDs older than the retention period."""
        now = _aware(now)
        for result in included:
            for item in result.items:
                self.items[item.id] = now
        for result in fetched:
            for item in result.items:
                if item.id in self.items:
                    self.items[item.id] = now
        self.prune(now)

    def prune(self, now: datetime, retention: timedelta = RETENTION) -> int:
        """Drop IDs not seen for longer than `retention`. Returns the number dropped."""
        cutoff = _aware(now) - retention
        stale = [k for k, last in self.items.items() if last < cutoff]
        for key in stale:
            del self.items[key]
        return len(stale)


def is_new(item: Item, state: SeenState, now: datetime, window: timedelta = WINDOW) -> bool:
    """Unseen and not older than `window`. Items without a date cannot be placed in time and
    are included once; items dated in the future (clock skew) count as new."""
    if item.id in state:
        return False
    return item.published is None or item.published >= _aware(now) - window


def select_new(
    results: Iterable[FetchResult], state: SeenState, now: datetime, window: timedelta = WINDOW
) -> list[FetchResult]:
    """The results with only their new items, one per result and in the same order.

    The status of each result is kept: an OK result without new items is "no new items", not a
    failure. An ID that occurs twice in this run (a feed listing an entry twice) is kept once.
    """
    taken: set[str] = set()

    def keep(item: Item) -> bool:
        if item.id in taken or not is_new(item, state, now, window):
            return False
        taken.add(item.id)
        return True

    return [result.only(keep) for result in results]
