"""The daily run end to end: fetch (stubbed RSS), dedupe, Claude (stub client), validation, the
Sources section, the written digest and the seen-state."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aggregator.config import Config, NewsletterSource, RssSource, Settings, WebSource
from aggregator.daily import DailySteps
from aggregator.dedupe import SeenState
from aggregator.pipeline import Outcome, RunRequest, run
from aggregator.render import parse_digest
from aggregator.sources import FetchResult, Item
from aggregator.store import read_digest

NOW = datetime(2026, 10, 19, 6, 3, tzinfo=UTC)
DAY = date(2026, 10, 19)
BODY = "## Tech\n- A lab released a model.\n## SAP\n- SAP shipped a release.\n"


def rss(source_id: str, name: str, topic: str | None = "Tech", enabled: bool = True) -> RssSource:
    return RssSource(
        id=source_id,
        name=name,
        type="rss",
        url=f"https://{source_id}.example.com/feed",
        topic=topic,
        enabled=enabled,
    )


SOURCES = [
    rss("sap-news", "SAP News Center", topic="SAP"),
    rss("hacker-news", "Hacker News"),
    rss("the-verge", "The Verge"),
    rss("old-blog", "Old Blog", enabled=False),
    NewsletterSource(id="letter", name="A Letter", type="newsletter", sender="news@example.com"),
    WebSource(id="site", name="A Site", type="web", url="https://site.example.com/"),
]


def item(source_id: str, n: int, age_hours: float = 1, topic: str | None = "Tech") -> Item:
    return Item(
        source_id=source_id,
        topic=topic,
        title=f"{source_id} story {n}",
        text=f"Text of {source_id} story {n}.",
        published=NOW - timedelta(hours=age_hours),
        id=f"{source_id}:{n}",
    )


class Feeds:
    """An RSS fetcher that serves canned results per source id and records the fetches."""

    def __init__(self, **results: FetchResult):
        self.results = results
        self.fetched: list[str] = []

    def __call__(self, source: RssSource) -> FetchResult:
        self.fetched.append(source.id)
        result = self.results.get(source.id)
        if isinstance(result, Exception):
            raise result
        return result or FetchResult.failure(source.id, "no parseable entries")


class Claude:
    """A stub Anthropic client that answers with canned texts in order."""

    def __init__(self, *answers: str | Exception):
        self.answers = list(answers) or [BODY]
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            model=kwargs["model"],
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=answer)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        )


def default_feeds() -> Feeds:
    return Feeds(
        **{
            "sap-news": FetchResult.success("sap-news", [item("sap-news", 1, topic="SAP")]),
            "hacker-news": FetchResult.success(
                "hacker-news", [item("hacker-news", 1), item("hacker-news", 2)]
            ),
            # Only an old item: OK, but no new items, so not a contributing source.
            "the-verge": FetchResult.success("the-verge", [item("the-verge", 1, age_hours=72)]),
        }
    )


@pytest.fixture
def config() -> Config:
    settings = Settings(categories=["Tech", "SAP", "Other"], models={"daily": "claude-daily-test"})
    return Config(settings=settings, sources=SOURCES, recipient="owner@example.com")


class Harness:
    def __init__(self, tmp_path: Path, config: Config, feeds: Feeds, claude: Claude):
        from aggregator.summarize import Summarizer

        self.config = config
        self.feeds = feeds
        self.claude = claude
        self.digests = tmp_path / "digests"
        self.state_path = tmp_path / "state" / "seen.json"
        self.log = io.StringIO()
        self.out = io.StringIO()
        self.daily = DailySteps(
            config,
            summarizer=Summarizer(config.settings, claude, sleep=lambda s: None),
            fetchers={"rss": feeds},
            state_path=self.state_path,
            clock=lambda: NOW,
            log=self.log,
        )

    def run(self, **request) -> Outcome:
        request = RunRequest(**({"level": "daily", "day": DAY} | request))
        return run(
            request,
            self.config,
            steps=self.daily.steps(),
            digests_dir=self.digests,
            now=NOW,
            out=self.out,
            log=self.log,
        )

    def seen(self) -> dict[str, str]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))["items"]


def harness(tmp_path, config, feeds=None, claude=None) -> Harness:
    return Harness(tmp_path, config, feeds or default_feeds(), claude or Claude())


# --- happy path ---------------------------------------------------------------------------------


def test_daily_run_writes_a_valid_digest_with_the_sources_section(tmp_path, config):
    h = harness(tmp_path, config)

    assert h.run() is Outcome.WRITTEN

    digest = read_digest("daily", "2026-10-19", h.digests)
    assert digest.meta.model == "claude-daily-test"
    assert digest.meta.inputs == ["sap-news", "hacker-news"]  # contributing sources only
    assert digest.body == BODY + "\n## Sources\n- SAP News Center\n- Hacker News\n"


def test_only_enabled_rss_sources_are_fetched(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()
    assert h.feeds.fetched == ["sap-news", "hacker-news", "the-verge"]
    assert "skipped sources without a fetcher yet: letter, site" in h.log.getvalue()


def test_claude_gets_the_new_items_with_their_topic_tags(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()

    (call,) = h.claude.calls
    user = call["messages"][0]["content"]
    assert user.count("<document ") == 3  # the old Verge item is outside the 48h window
    assert 'source="sap-news" topic="SAP"' in user
    assert "hacker-news story 2" in user and "the-verge" not in user
    assert call["model"] == "claude-daily-test"


def test_items_are_marked_as_seen_after_the_write(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()
    assert set(h.seen()) == {"sap-news:1", "hacker-news:1", "hacker-news:2"}
    assert f"updated {h.state_path}" in h.log.getvalue()


def test_the_next_day_only_summarizes_new_items(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()

    tomorrow = harness(tmp_path, config, claude=Claude())
    tomorrow.feeds.results["hacker-news"] = FetchResult.success(
        "hacker-news", [item("hacker-news", 2), item("hacker-news", 3)]
    )
    tomorrow.run(day=DAY + timedelta(days=1))

    user = tomorrow.claude.calls[0]["messages"][0]["content"]
    assert "hacker-news story 3" in user
    assert "hacker-news story 2" not in user and "sap-news story 1" not in user
    body = read_digest("daily", "2026-10-20", tomorrow.digests).body
    assert body.endswith("## Sources\n- Hacker News\n")


# --- dry run ------------------------------------------------------------------------------------


def test_dry_run_prints_the_digest_and_writes_no_digest_or_state(tmp_path, config):
    h = harness(tmp_path, config)

    assert h.run(dry_run=True) is Outcome.DRY_RUN

    digest = parse_digest(h.out.getvalue())
    assert digest.body.endswith("## Sources\n- SAP News Center\n- Hacker News\n")
    assert not h.digests.exists()
    assert not h.state_path.exists()


def test_dry_run_does_not_touch_an_existing_state(tmp_path, config):
    h = harness(tmp_path, config)
    SeenState(items={"other:1": NOW}).save(h.state_path)
    before = h.state_path.read_bytes()

    h.run(dry_run=True)

    assert h.state_path.read_bytes() == before


# --- validation results -------------------------------------------------------------------------


def test_over_limit_twice_is_truncated_and_flagged(tmp_path, config):
    long = "## Tech\n" + "".join(f"- Bullet {i}.\n" for i in range(12))
    h = harness(tmp_path, config, claude=Claude(long, long))

    assert h.run() is Outcome.WRITTEN

    log = h.log.getvalue()
    assert "retried for: The digest has 12 bullets" in log
    assert "warning: digest still had 12 bullets after the retry, truncated to 10" in log
    body = read_digest("daily", "2026-10-19", h.digests).body
    assert body.count("- Bullet") == 10
    assert body.endswith("## Sources\n- SAP News Center\n- Hacker News\n")


def test_token_usage_is_logged(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()
    assert "claude: claude-daily-test, 1 answer(s), 1000 input / 100 output tokens" in (
        h.log.getvalue()
    )


# --- failures -----------------------------------------------------------------------------------


def test_no_new_items_produces_no_digest_and_an_error(tmp_path, config):
    feeds = Feeds(
        **{
            "sap-news": FetchResult.success("sap-news", [item("sap-news", 1, age_hours=100)]),
            "hacker-news": FetchResult.failure("hacker-news", "timed out"),
        }
    )
    h = harness(tmp_path, config, feeds=feeds)

    outcome = h.run()

    assert outcome is Outcome.NO_INPUTS and outcome.is_error
    assert h.claude.calls == []
    assert not h.digests.exists() and not h.state_path.exists()
    log = h.log.getvalue()
    assert "warning: source hacker-news failed: timed out" in log
    assert "error: no inputs for daily digest 2026-10-19" in log


def test_one_failing_source_does_not_fail_the_run(tmp_path, config):
    feeds = default_feeds()
    feeds.results["sap-news"] = RuntimeError("boom")
    h = harness(tmp_path, config, feeds=feeds)

    assert h.run() is Outcome.WRITTEN

    digest = read_digest("daily", "2026-10-19", h.digests)
    assert digest.meta.inputs == ["hacker-news"]
    assert digest.body.endswith("## Sources\n- Hacker News\n")
    assert "warning: source sap-news failed: RuntimeError: boom" in h.log.getvalue()


def test_claude_failure_skips_the_digest_and_marks_nothing_seen(tmp_path, config):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx2.Response(401, request=request), body=None
    )
    h = harness(tmp_path, config, claude=Claude(error))

    outcome = h.run()

    assert outcome is Outcome.FAILED
    assert "error: daily digest 2026-10-19 failed, skipped: Claude API error" in h.log.getvalue()
    assert not h.digests.exists() and not h.state_path.exists()


def test_missing_api_key_is_a_failed_run(tmp_path, config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    h = harness(tmp_path, config)
    h.daily.summarizer._client = None  # use the real client factory

    assert h.run() is Outcome.FAILED
    assert "ANTHROPIC_API_KEY is not set" in h.log.getvalue()


def test_broken_seen_state_fails_the_run_before_fetching(tmp_path, config):
    h = harness(tmp_path, config)
    h.state_path.parent.mkdir(parents=True)
    h.state_path.write_text("{not json", encoding="utf-8")

    assert h.run() is Outcome.FAILED
    assert h.feeds.fetched == []
    assert "invalid seen-state" in h.log.getvalue()
    assert h.state_path.read_text(encoding="utf-8") == "{not json"  # never silently reset


def test_existing_digest_is_not_fetched_again(tmp_path, config):
    h = harness(tmp_path, config)
    h.run()

    again = harness(tmp_path, config)
    assert again.run() is Outcome.EXISTS
    assert again.feeds.fetched == [] and again.claude.calls == []
