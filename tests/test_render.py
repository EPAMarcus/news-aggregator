from datetime import UTC, datetime, timedelta, timezone

import pytest

from aggregator.config import RssSource
from aggregator.dedupe import SeenState, select_new
from aggregator.render import (
    Digest,
    DigestFormatError,
    FrontMatter,
    SourceWarning,
    contributing_sources,
    parse_digest,
    render_digest,
    render_sources_section,
    with_sources_section,
)
from aggregator.sources import FetchResult, FetchStatus, Item

GENERATED = datetime(2026, 10, 15, 6, 3, 12, tzinfo=UTC)


def make_digest(**overrides) -> Digest:
    meta = {
        "level": "daily",
        "period": "2026-10-15",
        "generated_at": GENERATED,
        "model": "claude-sonnet-5-5",
        "inputs": ["heise", "nyt-tech"],
    } | overrides
    return Digest(meta=FrontMatter(**meta), body="## Tech\n- Something happened.\n")


def test_render_writes_front_matter_then_body():
    text = render_digest(make_digest())
    assert text == (
        "---\n"
        "level: daily\n"
        "period: '2026-10-15'\n"
        "generated_at: '2026-10-15T06:03:12Z'\n"
        "model: claude-sonnet-5-5\n"
        "inputs:\n"
        "- heise\n"
        "- nyt-tech\n"
        "---\n"
        "\n"
        "## Tech\n"
        "- Something happened.\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "plain",
        "## Heading\n\n---\n\nA horizontal rule above.\n",
        "Ünïcödé, “quotes” and emoji-free text\n",
        "\n\nLeading blank lines\n",
    ],
)
def test_round_trip(body):
    digest = make_digest().model_copy(update={"body": body})
    assert parse_digest(render_digest(digest)) == digest


def test_round_trip_keeps_non_utc_offset():
    digest = make_digest(generated_at=GENERATED.astimezone(timezone(timedelta(hours=2))))
    parsed = parse_digest(render_digest(digest))
    assert parsed.meta.generated_at == digest.meta.generated_at
    assert parsed.meta.generated_at.utcoffset() == timedelta(hours=2)


def test_parse_accepts_crlf_and_unquoted_values():
    text = (
        "---\r\nlevel: yearly\r\nperiod: 2026\r\n"
        "generated_at: 2026-12-31T21:00:00+00:00\r\nmodel: m\r\n---\r\n\r\nBody\r\n"
    )
    digest = parse_digest(text)
    assert digest.meta.period == "2026"
    assert digest.meta.generated_at == datetime(2026, 12, 31, 21, tzinfo=UTC)
    assert digest.meta.inputs == []
    assert digest.body == "Body\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no front matter\n", "missing front matter"),
        ("---\nlevel: daily\n", "unterminated"),
        ("---\n---\n\nbody", "must be a mapping"),
        ("---\n- a list\n---\n", "must be a mapping"),
        ("---\nlevel: [unclosed\n---\n", "invalid YAML"),
    ],
)
def test_parse_rejects_malformed_documents(text, message):
    with pytest.raises(DigestFormatError, match=message):
        parse_digest(text)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"level": "hourly"}, "level"),
        ({"period": "2026-W42"}, "is not a daily period label"),  # wrong level for the label
        ({"period": "2026-02-30"}, "not an existing daily period"),
        ({"model": " "}, "model"),
        ({"generated_at": "2026-10-15T06:00:00"}, "generated_at"),  # no time zone
        ({"extra": "x"}, "extra"),
    ],
)
def test_parse_rejects_invalid_front_matter(overrides, field):
    data = make_digest().meta.model_dump(mode="json") | overrides
    lines = "".join(
        f"{k}: {v!r}\n" if isinstance(v, str) else f"{k}: {v}\n" for k, v in data.items()
    )
    with pytest.raises(DigestFormatError, match=field):
        parse_digest(f"---\n{lines}---\n\nbody\n")


# --- daily Sources section ----------------------------------------------------------------------


def rss(source_id: str, name: str) -> RssSource:
    return RssSource(id=source_id, name=name, type="rss", url=f"https://{source_id}.example.com/")


SOURCES = [
    rss("sap-news", "SAP News Center"),
    rss("hacker-news", "Hacker News"),
    rss("the-verge", "The Verge"),
    rss("new-stack", "The New Stack"),
]


def entry(source_id: str, n: int = 1) -> Item:
    return Item(
        source_id=source_id,
        topic=None,
        title=f"t{n}",
        text="x",
        published=None,
        id=f"{source_id}:{n}",
    )


def ok(source_id: str, items: int) -> FetchResult:
    return FetchResult(source_id, FetchStatus.OK, [entry(source_id, n) for n in range(items)])


def test_contributing_sources_are_exactly_those_with_new_items():
    results = [
        ok("hacker-news", 3),
        ok("sap-news", 1),
        ok("the-verge", 0),  # OK, but no new items after dedupe
        FetchResult.failure("new-stack", "timed out"),
    ]
    assert [s.id for s in contributing_sources(SOURCES, results)] == ["sap-news", "hacker-news"]


def test_contributing_sources_after_dedupe_drops_sources_whose_items_were_seen():
    fetched = [ok("hacker-news", 2), ok("sap-news", 1)]
    state = SeenState(items={"sap-news:0": GENERATED})
    fresh = select_new(fetched, state, GENERATED)
    assert [s.id for s in contributing_sources(SOURCES, fresh)] == ["hacker-news"]


def test_contributing_sources_ignores_unknown_ids_and_keeps_config_order():
    results = [ok("new-stack", 1), ok("removed-source", 1), ok("sap-news", 1)]
    assert [s.id for s in contributing_sources(SOURCES, results)] == ["sap-news", "new-stack"]


def test_sources_section_lists_names_only():
    section = render_sources_section([SOURCES[0], SOURCES[1]])
    assert section == "## Sources\n- SAP News Center\n- Hacker News\n"


def test_sources_section_has_a_slot_for_health_warnings():
    warning = SourceWarning("The Verge", "failed 3 runs in a row. Fix or remove the source.")
    section = render_sources_section([SOURCES[1]], [warning])
    assert section == (
        "## Sources\n"
        "- Hacker News\n"
        "\n"
        "### Warnings\n"
        "- The Verge: failed 3 runs in a row. Fix or remove the source.\n"
    )


def test_warnings_without_contributing_sources():
    section = render_sources_section([], [SourceWarning("The Verge", "stale")])
    assert section == "## Sources\n### Warnings\n- The Verge: stale\n"


def test_empty_sources_section_is_empty():
    assert render_sources_section([]) == ""


def test_names_are_kept_on_one_line():
    source = rss("odd", "Odd\nName  here")
    assert render_sources_section([source]) == "## Sources\n- Odd Name here\n"


def test_sources_section_goes_at_the_bottom_of_the_body():
    body = "## Tech\n- Something happened.\n\n\n"
    section = render_sources_section([SOURCES[1]])
    assert with_sources_section(body, section) == (
        "## Tech\n- Something happened.\n\n## Sources\n- Hacker News\n"
    )
    assert with_sources_section(body, "") == "## Tech\n- Something happened.\n"


def test_digest_with_sources_section_round_trips():
    body = with_sources_section("## Tech\n- x\n", render_sources_section([SOURCES[1]]))
    digest = make_digest().model_copy(update={"body": body})
    assert parse_digest(render_digest(digest)).body == body
