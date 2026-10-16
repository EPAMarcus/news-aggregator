from pathlib import Path
from textwrap import dedent

import pytest

from aggregator.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

SETTINGS = """
    timezone: Europe/Berlin
    recipient: owner@example.com
    categories: [Tech, SAP & Enterprise, Other]
"""

SOURCES = """
    sources:
      - {id: a-feed, name: A Feed, type: rss, url: "https://example.com/feed", topic: Tech/AI}
"""


def write_config(tmp_path: Path, settings: str = SETTINGS, sources: str = SOURCES) -> Path:
    (tmp_path / "settings.yaml").write_text(dedent(settings))
    (tmp_path / "sources.yaml").write_text(dedent(sources))
    return tmp_path


def test_committed_sample_config_is_valid():
    config = load_config(REPO_ROOT / "config")
    assert config.settings.timezone == "Europe/Berlin"
    assert config.enabled_sources


def test_minimal_config_gets_defaults(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.settings.stale_after_days == 30
    assert config.settings.limits.daily_bullets == 10
    assert config.settings.models.weekly == "claude-sonnet-5-5"
    assert config.sources[0].category == "Tech"


def test_unknown_top_level_topic_fails_with_clear_error(tmp_path):
    sources = """
        sources:
          - {id: a, name: A, type: rss, url: "https://example.com/f", topic: Sports/Football}
    """
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, sources=sources))
    message = str(exc.value)
    assert "'a'" in message
    assert "'Sports'" in message
    assert "Tech, SAP & Enterprise, Other" in message


def test_sub_categories_are_free_form(tmp_path):
    sources = """
        sources:
          - {id: a, name: A, type: rss, url: "https://example.com/f", topic: Tech/Any Thing/Deep}
          - {id: b, name: B, type: rss, url: "https://example.com/g", topic: SAP & Enterprise}
          - {id: c, name: C, type: rss, url: "https://example.com/h"}
    """
    config = load_config(write_config(tmp_path, sources=sources))
    assert [s.category for s in config.sources] == ["Tech", "SAP & Enterprise", None]


def test_disabled_sources_are_still_validated(tmp_path):
    sources = """
        sources:
          - {id: a, name: A, type: rss, url: "https://example.com/f", topic: Nope, enabled: false}
    """
    with pytest.raises(ConfigError, match="Nope"):
        load_config(write_config(tmp_path, sources=sources))


def test_duplicate_source_ids_are_rejected(tmp_path):
    sources = """
        sources:
          - {id: dup, name: A, type: rss, url: "https://example.com/f"}
          - {id: dup, name: B, type: newsletter, from: news@example.com}
    """
    with pytest.raises(ConfigError, match="duplicate source id 'dup'"):
        load_config(write_config(tmp_path, sources=sources))


@pytest.mark.parametrize(
    ("entry", "missing"),
    [
        ("{id: a, name: A, type: rss}", "url"),
        ("{id: a, name: A, type: web}", "url"),
        ("{id: a, name: A, type: newsletter}", "from"),
    ],
)
def test_missing_type_specific_fields_are_rejected(tmp_path, entry, missing):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, sources=f"sources:\n  - {entry}\n"))
    message = str(exc.value)
    assert "source 'a'" in message
    assert missing in message


def test_field_of_another_type_is_rejected(tmp_path):
    entry = '{id: a, name: A, type: newsletter, from: n@example.com, url: "https://example.com"}'
    with pytest.raises(ConfigError, match="url"):
        load_config(write_config(tmp_path, sources=f"sources:\n  - {entry}\n"))


def test_unknown_source_type_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="source 'a'"):
        load_config(write_config(tmp_path, sources="sources:\n  - {id: a, name: A, type: ftp}\n"))


def test_web_and_newsletter_fields(tmp_path):
    sources = """
        sources:
          - {id: w, name: W, type: web, url: "https://example.com/n", selector: "article a"}
          - {id: n, name: N, type: newsletter, from: News@Example.com}
    """
    web, newsletter = load_config(write_config(tmp_path, sources=sources)).sources
    assert web.selector == "article a"
    assert newsletter.sender == "news@example.com"


@pytest.mark.parametrize(
    "settings_line",
    [
        "timezone: Mars/Olympus",
        "recipient: not-an-email",
        "categories: []",
        "categories: [Tech, Tech]",
        "categories: [Tech/AI]",
        "stale_after_days: 0",
        "limits: {daily_bullets: -1}",
        "unexpected_key: 1",
    ],
)
def test_invalid_settings_are_rejected(tmp_path, settings_line):
    settings = dedent(SETTINGS) + settings_line + "\n"
    # a later duplicate key would silently override in YAML, so drop the valid line it replaces
    key = settings_line.split(":", 1)[0]
    lines = [ln for ln in settings.splitlines() if not ln.strip().startswith(f"{key}:")]
    settings = "\n".join([*lines, settings_line])
    with pytest.raises(ConfigError, match="settings.yaml"):
        load_config(write_config(tmp_path, settings=settings))


def test_all_source_problems_are_reported_together(tmp_path):
    sources = """
        sources:
          - {id: dup, name: A, type: rss, url: "https://example.com/f", topic: Nope}
          - {id: dup, name: B, type: rss, url: "https://example.com/g", topic: Other}
    """
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, sources=sources))
    assert "duplicate source id" in str(exc.value)
    assert "unknown top-level category" in str(exc.value)


def test_non_http_url_is_rejected(tmp_path):
    sources = "sources:\n  - {id: a, name: A, type: rss, url: 'ftp://example.com/f'}\n"
    with pytest.raises(ConfigError, match="http"):
        load_config(write_config(tmp_path, sources=sources))


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path)
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write_config(tmp_path, sources="sources: [unclosed"))
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write_config(tmp_path, sources="- just\n- a list\n"))
