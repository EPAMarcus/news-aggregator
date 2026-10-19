import threading
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aggregator.config import NewsletterSource, RssSource
from aggregator.sources import FetchResult, FetchStatus, Item, fetch_all, fetch_source, rss
from aggregator.sources.text import html_to_text

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def source(id="example", topic="Tech/AI", url="https://example.com/feed"):
    return RssSource(id=id, name=id.title(), type="rss", url=url, topic=topic)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def serve(data: bytes):
    """A replacement for http.get that returns `data` and records the request."""
    calls = []

    def get(url, timeout):
        calls.append((url, timeout))
        return data

    get.calls = calls
    return get


# --- RSS and Atom ------------------------------------------------------------------------------


def test_rss2_fixture():
    result = rss.parse(source(), fixture("rss2.xml"))

    assert result.status is FetchStatus.OK
    assert result.error is None
    first, second, undated = result.items

    assert first == Item(
        source_id="example",
        topic="Tech/AI",
        title="First story & more",
        text="Full text of the first story.\n\nSecond paragraph.",  # content:encoded, no script
        published=datetime(2026, 10, 15, 6, 30, tzinfo=UTC),  # +0200 normalized to UTC
        id="example:example-guid-1",
        url="https://example.com/first",
    )
    # No content:encoded: the summary is used, HTML stripped. No guid: the link is the id.
    assert second.text == "Only a summary here."
    assert second.id == "example:https://example.com/second"
    assert second.published == datetime(2026, 10, 14, 22, tzinfo=UTC)
    # No date, link or guid: still an item, with a title-based stable id.
    assert undated.published is None
    assert undated.url is None
    assert undated.id.startswith("example:sha1-")


def test_atom_fixture():
    result = rss.parse(source(topic=None), fixture("atom.xml"))

    assert result.ok
    full, updated_only = result.items
    assert full.title == "Atom with full content"
    assert full.text == "The full Atom content."  # <content> wins over <summary>
    assert full.id == "example:tag:blog.example.com,2026:post-1"
    assert full.url == "https://blog.example.com/post-1"
    assert full.published == datetime(2026, 10, 15, 9, tzinfo=UTC)  # published, not updated
    assert full.topic is None
    # Only <updated>: used as the date. Plain text summary: whitespace normalized.
    assert updated_only.published == datetime(2026, 10, 14, 16, 15, tzinfo=UTC)
    assert updated_only.text == "Plain text summary over two lines."


def test_ids_are_stable_across_fetches():
    first = [i.id for i in rss.parse(source(), fixture("rss2.xml")).items]
    again = [i.id for i in rss.parse(source(), fixture("rss2.xml")).items]
    assert first == again
    assert len(set(first)) == len(first)


def test_ids_are_unique_across_sources():
    a = rss.parse(source(id="a"), fixture("atom.xml")).items[0]
    b = rss.parse(source(id="b"), fixture("atom.xml")).items[0]
    assert a.id != b.id


def test_fetch_downloads_the_source_url():
    get = serve(fixture("atom.xml"))
    result = rss.fetch(source(url="https://blog.example.com/feed"), get=get)
    assert result.ok
    assert get.calls == [("https://blog.example.com/feed", rss.http.TIMEOUT_SECONDS)]


# --- broken and empty feeds --------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\xff\xfe garbage",
        b"<rss><channel><item>",
        b'<?xml version="1.0"?><nope/>',
        fixture("not-a-feed.html"),
    ],
)
def test_broken_feed_does_not_raise_and_fails(data):
    result = rss.parse(source(), data)
    assert result.status is FetchStatus.FAILED
    assert result.items == []
    assert "no parseable entries" in result.error


def test_valid_but_empty_feed_is_a_failure():
    # spec 3: "returns no parseable entries at all" is a failure, even for a well-formed feed
    result = rss.parse(source(), fixture("empty.xml"))
    assert not result.ok
    assert result.error == "no parseable entries"


def test_partly_broken_feed_keeps_the_readable_entries():
    result = rss.parse(source(), fixture("truncated.xml"))
    assert result.ok
    assert [i.title for i in result.items][0] == "Readable before the break"


def test_entries_without_title_and_text_are_skipped():
    data = b"""<rss version="2.0"><channel><title>t</title>
        <item><link>https://example.com/x</link></item>
        <item><description>Text only, no title.</description></item>
    </channel></rss>"""
    result = rss.parse(source(), data)
    assert [i.title for i in result.items] == ["Text only, no title."]


# --- failure vs. no new items ------------------------------------------------------------------


def test_no_new_items_is_not_a_failure():
    result = rss.parse(source(), fixture("rss2.xml"))
    seen = {item.id for item in result.items}

    unseen = result.only(lambda item: item.id not in seen)

    assert unseen.status is FetchStatus.OK
    assert unseen.ok and unseen.no_new_items
    assert unseen.error is None


def test_no_parseable_entries_is_a_failure_not_no_new_items():
    result = rss.parse(source(), fixture("empty.xml"))
    assert result.status is FetchStatus.FAILED
    assert not result.no_new_items


def test_success_without_items_is_a_failure():
    assert FetchResult.success("x", []).status is FetchStatus.FAILED


# --- isolation and timeouts --------------------------------------------------------------------


def raising(exc):
    def fetcher(src):
        raise exc

    return fetcher


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), "HTTPError: HTTP Error 404"),
        (urllib.error.URLError("name resolution failed"), "URLError"),
        (TimeoutError(), "TimeoutError"),
        (ValueError("bad"), "ValueError: bad"),
    ],
)
def test_fetch_errors_become_failed_results(exc, message):
    result = fetch_source(source(), {"rss": raising(exc)})
    assert result.status is FetchStatus.FAILED
    assert result.source_id == "example"
    assert message in result.error


def test_one_failing_source_does_not_affect_the_others():
    good = source(id="good")
    bad = source(id="bad")
    feed = fixture("atom.xml")

    def fetcher(src):
        if src.id == "bad":
            raise ConnectionResetError("reset")
        return rss.parse(src, feed)

    results = fetch_all([bad, good, bad], {"rss": fetcher})

    assert [(r.source_id, r.ok) for r in results] == [
        ("bad", False),
        ("good", True),
        ("bad", False),
    ]
    assert len(results[1].items) == 2


def test_hanging_source_times_out():
    release = threading.Event()

    def hangs(src):
        release.wait(5)
        return FetchResult.success(src.id, [])

    try:
        result = fetch_source(source(), {"rss": hangs}, timeout=0.05)
    finally:
        release.set()
    assert result.status is FetchStatus.FAILED
    assert "timed out" in result.error


def test_source_type_without_fetcher_fails_cleanly():
    newsletter = NewsletterSource(id="n", name="N", type="newsletter", sender="a@example.com")
    result = fetch_source(newsletter)
    assert result.status is FetchStatus.FAILED
    assert "no fetcher for type 'newsletter'" in result.error


def test_default_fetchers_download_rss_feeds(monkeypatch):
    get = serve(fixture("rss2.xml"))
    monkeypatch.setattr(rss.http, "get", get)
    result = fetch_source(source(url="https://example.com/rss"))
    assert result.ok
    assert len(result.items) == 3
    assert get.calls[0][0] == "https://example.com/rss"


# --- HTML to text ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>a</p><p>b</p>", "a\n\nb"),
        ("x &amp; y&nbsp;z", "x & y z"),
        ("<style>p{}</style><p>kept</p><script>dropped()</script>", "kept"),
        ("line<br>break", "line break"),  # a soft break inside a paragraph
        ("  lots   of\n  space  ", "lots of space"),
        ("<ul><li>one</li><li>two</li></ul>", "one\n\ntwo"),
        ("", ""),
    ],
)
def test_html_to_text(html, text):
    assert html_to_text(html) == text
