"""The common `Item` model and the fetcher result contract.

Health semantics (spec 3, "Source health"):

- A source **fails** when the fetch errors, times out, or returns no parseable entries at all
  (including a valid feed that is empty). Its result has `status == FAILED` and an `error`.
- Otherwise the result is `OK`, with at least one parsed item. Which of them are *new* is decided
  by dedupe (#8) through `FetchResult.only`; an OK result left without items is "no new items"
  (`no_new_items`), and never a failure.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from aggregator.config import Source

log = logging.getLogger(__name__)

# Upper bound for one source, including parsing. The HTTP download has its own, shorter limit.
SOURCE_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class Item:
    source_id: str
    topic: str | None  # the source's topic tag, a clustering hint for the LLM
    title: str
    text: str  # plain text: the full text if the source has it, otherwise a summary
    published: datetime | None  # timezone-aware (UTC); None if the source gives no date
    id: str  # stable across runs, unique across sources: "<source_id>:<source-specific id>"
    url: str | None = None  # kept for reference; digests do not contain links (spec 8)


class FetchStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    status: FetchStatus
    items: list[Item] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK

    def only(self, keep: Callable[[Item], bool]) -> FetchResult:
        """The same result with only the items for which `keep` is true, e.g. the unseen ones
        inside the 48h window (#8). The status does not change: an OK result whose items are all
        filtered out means "no new items", which is not a failure."""
        return replace(self, items=[item for item in self.items if keep(item)])

    @property
    def no_new_items(self) -> bool:
        return self.ok and not self.items

    @classmethod
    def success(cls, source_id: str, items: list[Item]) -> FetchResult:
        if not items:
            return cls.failure(source_id, "no parseable entries")
        return cls(source_id, FetchStatus.OK, items)

    @classmethod
    def failure(cls, source_id: str, error: str) -> FetchResult:
        return cls(source_id, FetchStatus.FAILED, [], error)


# Called only with sources of its own type (e.g. RssSource). It may raise anything;
# fetch_source turns exceptions into failed results.
Fetcher = Callable[[Any], FetchResult]


def _default_fetchers() -> dict[str, Fetcher]:
    from aggregator.sources import rss

    return {"rss": rss.fetch}


def fetch_source(
    source: Source,
    fetchers: Mapping[str, Fetcher] | None = None,
    timeout: float = SOURCE_TIMEOUT_SECONDS,
) -> FetchResult:
    """Fetch one source within `timeout` seconds. Never raises (except for KeyboardInterrupt
    and the like): errors and timeouts become a failed result for that source.

    The fetcher runs in a daemon thread. A fetcher that hangs past the timeout cannot be killed,
    but it no longer blocks the run, and it ends with the process.
    """
    fetchers = _default_fetchers() if fetchers is None else fetchers
    fetcher = fetchers.get(source.type)
    if fetcher is None:
        return FetchResult.failure(source.id, f"no fetcher for type {source.type!r} yet")

    outcome: list[FetchResult | Exception] = []

    def target() -> None:
        try:
            outcome.append(fetcher(source))
        except Exception as exc:  # isolation: any error in one source is that source's failure
            outcome.append(exc)

    worker = threading.Thread(target=target, name=f"fetch-{source.id}", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        log.warning("source %s timed out after %gs", source.id, timeout)
        return FetchResult.failure(source.id, f"timed out after {timeout:g}s")

    result = outcome[0]
    if isinstance(result, Exception):
        log.warning("source %s failed", source.id, exc_info=result)
        return FetchResult.failure(source.id, _describe(result))
    if result.source_id != source.id:
        return FetchResult.failure(source.id, f"fetcher returned result for {result.source_id!r}")
    return result


def fetch_all(
    sources: Iterable[Source],
    fetchers: Mapping[str, Fetcher] | None = None,
    timeout: float = SOURCE_TIMEOUT_SECONDS,
) -> list[FetchResult]:
    """Fetch every source in order, one result per source."""
    fetchers = _default_fetchers() if fetchers is None else fetchers
    return [fetch_source(source, fetchers, timeout) for source in sources]


def _describe(exc: Exception) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
