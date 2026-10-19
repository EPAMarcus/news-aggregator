from datetime import UTC, datetime, timedelta, timezone

import pytest

from aggregator.render import Digest, DigestFormatError, FrontMatter, parse_digest, render_digest

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
