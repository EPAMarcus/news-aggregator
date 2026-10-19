"""RSS and Atom feeds via `feedparser` (architecture 3.2).

The feed is downloaded by `http.get` (time and size limits, polite user agent) and parsed from
bytes, so feedparser never does network I/O itself. Each entry's text is the full content if the
feed has it (`content:encoded`, Atom `<content>`), otherwise the summary.
"""

from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, datetime

import feedparser

from aggregator.config import RssSource
from aggregator.sources import http
from aggregator.sources.base import FetchResult, Item
from aggregator.sources.text import html_to_text

_HTML_TYPES = ("text/html", "application/xhtml+xml", "html", "xhtml")


def fetch(source: RssSource, get: http.Get | None = None) -> FetchResult:
    """Download and parse the feed of `source`. Raises on download errors; `fetch_source`
    turns them into a failed result."""
    get = get or http.get
    return parse(source, get(source.url, http.TIMEOUT_SECONDS))


def parse(source: RssSource, data: bytes) -> FetchResult:
    """Turn raw feed bytes into a result. Never raises for malformed input."""
    feed = feedparser.parse(data)
    items = [item for entry in feed.entries if (item := _to_item(source, entry)) is not None]
    if not items:
        if feed.bozo and not feed.entries:
            reason = getattr(feed, "bozo_exception", None) or "not a feed"
            return FetchResult.failure(source.id, f"no parseable entries ({reason})")
        return FetchResult.failure(source.id, "no parseable entries")
    # feedparser recovers from many errors (bozo); items it could read are still used.
    return FetchResult.success(source.id, items)


def _to_item(source: RssSource, entry) -> Item | None:
    title = html_to_text(entry.get("title", "")).strip()
    text = _entry_text(entry)
    if not title and not text:
        return None
    url = entry.get("link") or None
    return Item(
        source_id=source.id,
        topic=source.topic,
        title=title or text.split("\n", 1)[0][:120],
        text=text,
        published=_published(entry),
        id=f"{source.id}:{_stable_id(entry, url, title)}",
        url=url,
    )


def _entry_text(entry) -> str:
    # Prefer the longest full-content block; fall back to the summary.
    contents = [c for c in entry.get("content", []) if c.get("value")]
    if contents:
        best = max(contents, key=lambda c: len(c["value"]))
        return _to_text(best["value"], best.get("type", ""))
    summary = entry.get("summary", "")
    detail = entry.get("summary_detail") or {}
    return _to_text(summary, detail.get("type", "text/html"))


def _to_text(value: str, content_type: str) -> str:
    if content_type in _HTML_TYPES or "<" in value:
        return html_to_text(value)
    return " ".join(value.split())


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            # feedparser normalizes to UTC struct_time
            return datetime.fromtimestamp(calendar.timegm(parsed), UTC)
    return None


def _stable_id(entry, url: str | None, title: str) -> str:
    """The feed's own id (guid / Atom id) if present, else the link, else a hash of the title."""
    raw = entry.get("id") or url
    if raw:
        return raw.strip()
    return "sha1-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
