from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aggregator.config import Settings
from aggregator.daily import PromptError, daily_task, load_prompt, summarize_daily
from aggregator.sources import Item
from aggregator.summarize import DATA_TAG, Summarizer, SummarizerError

VALID = "## Tech\n### AI\n- A lab released a model.\n- Prices dropped.\n## SAP\n- SAP shipped.\n"


def bullets(n: int) -> str:
    return "## Tech\n" + "".join(f"- Bullet {i}.\n" for i in range(1, n + 1))


def reply(text: str):
    return SimpleNamespace(
        model="claude-daily-test",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


class StubClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return reply(outcome)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        categories=["Tech", "SAP", "Other"],
        models={"daily": "claude-daily-test"},
        limits={"daily_bullets": 5},
    )


def item(n: int = 1, **overrides) -> Item:
    values = dict(
        source_id="hacker-news",
        topic="Tech/AI",
        title=f"Story {n}",
        text=f"Text of story {n}.",
        published=datetime(2026, 10, 19, 5, tzinfo=UTC),
        id=f"hacker-news:{n}",
    )
    return Item(**(values | overrides))


ITEMS = [item(1), item(2, source_id="sap-news", topic="SAP", id="sap-news:2"), item(3, topic=None)]


def run(settings, *answers):
    client = StubClient(*answers)
    summarizer = Summarizer(settings, client, sleep=lambda s: None)
    return summarize_daily(ITEMS, settings, summarizer), client


# --- prompt -------------------------------------------------------------------------------------


def test_daily_task_fills_in_categories_and_limit(settings):
    task = daily_task(settings)
    assert "Tech, SAP, Other" in task
    assert "at most 5 bullets in total" in task
    assert "$" not in task  # every placeholder was filled in


def test_daily_task_states_the_clustering_and_merge_rules(settings):
    task = daily_task(settings)
    assert "`topic` attribute" in task and "strong hint" in task
    assert "same story, merge them into one bullet" in task
    assert "`## ` heading" in task and "`### ` sub-sections" in task
    assert "no `Sources` section" in task


def test_prompt_errors_are_clear(tmp_path):
    with pytest.raises(PromptError, match="cannot read"):
        load_prompt("daily", {}, tmp_path)
    (tmp_path / "daily.md").write_text("Use $unknown here", encoding="utf-8")
    with pytest.raises(PromptError, match="bad placeholder"):
        load_prompt("daily", {}, tmp_path)


def test_request_contains_the_task_and_every_item(settings):
    _, client = run(settings, VALID)

    user = client.calls[0]["messages"][0]["content"]
    assert user.startswith("<task>\nWrite today's daily news digest")
    data = user[user.index(f"<{DATA_TAG}>") :]
    assert 'source="hacker-news" topic="Tech/AI"' in data
    assert 'source="sap-news" topic="SAP"' in data
    assert data.count("<document ") == 3
    assert client.calls[0]["model"] == "claude-daily-test"


# --- validation and retry -----------------------------------------------------------------------


def test_valid_output_is_accepted_without_retry(settings):
    body, client = run(settings, VALID)

    assert len(client.calls) == 1
    assert body.text == VALID
    assert body.ok and body.attempts == 1
    assert body.problems == [] and body.retried_for == [] and body.truncated_from is None
    assert body.model == "claude-daily-test"


def test_over_limit_then_fixed_on_retry(settings):
    body, client = run(settings, bullets(7), bullets(5))

    assert len(client.calls) == 2
    assert body.text == bullets(5)
    assert body.ok and body.attempts == 2
    assert any("7 bullets" in p for p in body.retried_for)
    assert (body.input_tokens, body.output_tokens) == (200, 40)

    # The retry continues the conversation: same task and data, the rejected answer, and a
    # request that names the violation.
    first, retry = client.calls
    messages = retry["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0] == first["messages"][0]
    assert messages[1]["content"] == bullets(7).strip()
    assert "7 bullets" in messages[2]["content"] and "at most 5" in messages[2]["content"]
    assert retry["system"] == first["system"]


def test_over_limit_twice_is_truncated_and_flagged(settings):
    body, client = run(settings, bullets(8), bullets(7))

    assert len(client.calls) == 2  # exactly one retry
    assert body.text == bullets(5)  # the first five bullets of the second answer
    assert body.truncated_from == 7
    assert body.problems == []  # the truncated text itself is valid
    assert not body.ok  # but the run must flag it


def test_structure_problem_triggers_the_retry(settings):
    body, client = run(settings, "Here is your digest:\n\n" + VALID, VALID)

    assert len(client.calls) == 2
    assert body.ok and body.text == VALID
    assert any("introduction" in p for p in body.retried_for)
    assert "introduction" in client.calls[1]["messages"][2]["content"]


def test_structure_problem_left_after_retry_is_reported_not_fatal(settings):
    wrong = "## World\n- Something.\n"
    body, _ = run(settings, wrong, wrong)

    assert body.text == wrong
    assert body.truncated_from is None
    assert any("not an allowed section" in p for p in body.problems)
    assert not body.ok


def test_all_violations_are_named_in_the_retry(settings):
    _, client = run(settings, "Intro\n" + bullets(6), VALID)

    request = client.calls[1]["messages"][2]["content"]
    assert "6 bullets" in request and 'Found: "Intro"' in request


def test_api_failure_on_the_retry_propagates(settings):
    client = StubClient(bullets(9), RuntimeError("boom"))
    summarizer = Summarizer(settings, client, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        summarize_daily(ITEMS, settings, summarizer)


def test_no_items_is_a_summarizer_error(settings):
    summarizer = Summarizer(settings, StubClient(), sleep=lambda s: None)
    with pytest.raises(SummarizerError, match="no input documents"):
        summarize_daily([], settings, summarizer)
