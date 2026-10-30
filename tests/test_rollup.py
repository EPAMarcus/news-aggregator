import io
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from aggregator import rollup
from aggregator.config import Config, Settings, load_config
from aggregator.pipeline import Outcome, RunRequest, StepFailed, Steps, run
from aggregator.render import Digest, FrontMatter
from aggregator.rollup import (
    RollupSteps,
    collect,
    load_inputs,
    rollup_task,
    select_inputs,
    summarize_rollup,
    word_limits,
)
from aggregator.schedule import period_label
from aggregator.store import digest_path, read_digest, write_digest
from aggregator.summarize import Summarizer, SummarizerError


def write(root, level, period, body=None):
    meta = FrontMatter(
        level=level,
        period=period,
        generated_at=datetime(2026, 10, 15, 6, tzinfo=UTC),
        model="claude-sonnet-5-5",
        inputs=["a"],
    )
    write_digest(Digest(meta=meta, body=body or f"{level} {period}\n"), root)


def write_dailies(root, first, last):
    day = first
    while day <= last:
        write(root, "daily", day.isoformat())
        day += timedelta(days=1)


def dailies(first, last):
    return [
        ("daily", (first + timedelta(days=n)).isoformat()) for n in range((last - first).days + 1)
    ]


def test_weekly_reads_the_dailies_of_the_week(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 18), date(2026, 10, 26))  # Sunday before to Monday after
    assert select_inputs("weekly", "2026-W43", tmp_path) == dailies(
        date(2026, 10, 19), date(2026, 10, 25)
    )


def test_weekly_across_new_year(tmp_path):
    write_dailies(tmp_path, date(2025, 12, 29), date(2026, 1, 4))
    assert select_inputs("weekly", "2026-W01", tmp_path) == dailies(
        date(2025, 12, 29), date(2026, 1, 4)
    )


def test_weekly_skips_a_missing_daily(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 19), date(2026, 10, 25))
    digest_path("daily", "2026-10-21", tmp_path).unlink()
    selected = select_inputs("weekly", "2026-W43", tmp_path)
    assert ("daily", "2026-10-21") not in selected
    assert len(selected) == 6


def test_month_ending_mid_week(tmp_path):
    # October 2026: Thursday 1st to Saturday 31st; W41 to W43 lie fully inside.
    write_dailies(tmp_path, date(2026, 9, 28), date(2026, 11, 1))
    for week in ("2026-W40", "2026-W41", "2026-W42", "2026-W43", "2026-W44"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        *dailies(date(2026, 10, 1), date(2026, 10, 4)),
        ("weekly", "2026-W41"),
        ("weekly", "2026-W42"),
        ("weekly", "2026-W43"),
        *dailies(date(2026, 10, 26), date(2026, 10, 31)),
    ]


def test_month_ending_on_sunday(tmp_path):
    # May 2026: Friday 1st to Sunday 31st; the last week W22 lies fully inside.
    write_dailies(tmp_path, date(2026, 4, 27), date(2026, 5, 31))
    for week in ("2026-W18", "2026-W19", "2026-W20", "2026-W21", "2026-W22"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-05", tmp_path) == [
        *dailies(date(2026, 5, 1), date(2026, 5, 3)),
        ("weekly", "2026-W19"),
        ("weekly", "2026-W20"),
        ("weekly", "2026-W21"),
        ("weekly", "2026-W22"),
    ]


def test_month_with_a_missing_weekly_uses_its_dailies(tmp_path):
    write_dailies(tmp_path, date(2026, 10, 1), date(2026, 10, 31))
    for week in ("2026-W41", "2026-W43"):
        write(tmp_path, "weekly", week)
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        *dailies(date(2026, 10, 1), date(2026, 10, 4)),
        ("weekly", "2026-W41"),
        *dailies(date(2026, 10, 12), date(2026, 10, 18)),
        ("weekly", "2026-W43"),
        *dailies(date(2026, 10, 26), date(2026, 10, 31)),
    ]


def test_month_with_missing_dailies_skips_them(tmp_path):
    for day in ("2026-10-02", "2026-10-31"):
        write(tmp_path, "daily", day)
    write(tmp_path, "weekly", "2026-W42")
    assert select_inputs("monthly", "2026-10", tmp_path) == [
        ("daily", "2026-10-02"),
        ("weekly", "2026-W42"),
        ("daily", "2026-10-31"),
    ]


def test_month_ignores_a_weekly_of_a_partial_week(tmp_path):
    write(tmp_path, "weekly", "2026-W44")  # 26 Oct to 1 Nov
    assert select_inputs("monthly", "2026-10", tmp_path) == []


def test_yearly_reads_the_monthlies(tmp_path):
    for month in ("2025-12", "2026-01", "2026-03", "2027-01"):
        write(tmp_path, "monthly", month)
    assert select_inputs("yearly", "2026", tmp_path) == [
        ("monthly", "2026-01"),
        ("monthly", "2026-03"),
    ]


def test_daily_is_not_a_rollup(tmp_path):
    with pytest.raises(ValueError, match="not rollups"):
        select_inputs("daily", "2026-10-15", tmp_path)


def test_load_inputs_reads_the_digests(tmp_path):
    write(tmp_path, "daily", "2026-10-20")
    [loaded] = load_inputs("weekly", "2026-W43", tmp_path)
    assert (loaded.level, loaded.period) == ("daily", "2026-10-20")
    assert loaded.digest.body == "daily 2026-10-20\n"


def test_collect_fails_on_an_invalid_input(tmp_path):
    write(tmp_path, "daily", "2026-10-20")
    digest_path("daily", "2026-10-20", tmp_path).write_text("no front matter\n")
    request = RunRequest("weekly", date(2026, 10, 25))
    with pytest.raises(StepFailed, match="2026-10-20"):
        collect(request, None, tmp_path)


@pytest.fixture
def config():
    return load_config()


def test_no_inputs_ends_the_run_with_an_alert_outcome(tmp_path, config):
    def collect_step(request, cfg):
        return rollup.collect(request, cfg, tmp_path)

    outcome = run(
        RunRequest("monthly", date(2026, 10, 31)),
        config,
        steps=Steps(collect=collect_step),
        digests_dir=tmp_path,
    )
    assert outcome is Outcome.NO_INPUTS
    assert outcome.is_error
    assert not digest_path(
        "monthly", period_label("monthly", date(2026, 10, 31)), tmp_path
    ).exists()


# --- summary ------------------------------------------------------------------------------------

DAILY_BODY = (
    "## Tech\n- A lab released a model. ([Hacker News](<https://news.example/1>))\n"
    "\n## Sources\n- Hacker News\n"
)
VALID = "## Tech\nA lab released a model and prices fell.\n\n## SAP\n- SAP shipped a release.\n"


def words(n):
    return "## Tech\n" + " ".join(["word"] * n) + "\n"


def reply(text):
    return SimpleNamespace(
        model="claude-rollup-test",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


class StubClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return reply(outcome)

    def documents(self, call=0):
        """(level, period, content) of each document sent in a call."""
        content = self.calls[call]["messages"][0]["content"]
        return re.findall(
            r'<document index="\d+" level="(\w+)" period="([\w-]+)">\n<content>\n(.*?)\n</content>',
            content,
            re.S,
        )


@pytest.fixture
def settings():
    return Settings(
        categories=["Tech", "SAP", "Other"],
        models={"weekly": "claude-weekly-test", "monthly": "claude-monthly-test"},
        limits={"weekly_words": 20, "monthly_words": 40},
    )


def summarizer(settings, *answers):
    client = StubClient(*answers)
    return Summarizer(settings, client, sleep=lambda s: None), client


@pytest.fixture
def week(tmp_path):
    """Dailies of 2026-W43 (19 to 25 October) and of the days around it."""
    for day in ("2026-10-18", "2026-10-19", "2026-10-21", "2026-10-26"):
        write(tmp_path, "daily", day, DAILY_BODY)
    return tmp_path


def test_word_limits(settings):
    assert word_limits("weekly", settings) == (20, 20)  # at most
    assert word_limits("monthly", settings) == (40, 44)  # about


@pytest.mark.parametrize("level", ["weekly", "monthly"])
def test_the_prompts_fill_in_categories_and_limits(level):
    settings = load_config().settings
    target, maximum = word_limits(level, settings)
    task = rollup_task(level, settings)
    assert ", ".join(settings.categories) in task
    assert str(maximum) in task
    assert "$" not in task
    if level == "monthly":
        assert f"about {target} words" in task


def test_weekly_sends_only_the_dailies_of_the_week_without_links(week, settings):
    inputs = load_inputs("weekly", "2026-W43", week)
    summ, client = summarizer(settings, VALID)
    body = summarize_rollup("weekly", inputs, settings, summ)

    assert (body.text, body.model, body.attempts, body.problems) == (
        VALID,
        "claude-rollup-test",
        1,
        [],
    )
    assert client.calls[0]["model"] == "claude-weekly-test"
    assert client.documents() == [
        ("daily", "2026-10-19", "## Tech\n- A lab released a model.\n"),
        ("daily", "2026-10-21", "## Tech\n- A lab released a model.\n"),
    ]
    assert "news.example" not in client.calls[0]["messages"][0]["content"]
    assert "at most 20 words" in client.calls[0]["messages"][0]["content"]


def test_monthly_sends_the_weeklies_and_the_remaining_dailies(tmp_path, settings):
    write(tmp_path, "daily", "2026-10-02", DAILY_BODY)
    write(tmp_path, "weekly", "2026-W42", "## Tech\nA quiet week.\n")
    write(tmp_path, "daily", "2026-10-14", DAILY_BODY)  # inside W42: covered by the weekly
    inputs = load_inputs("monthly", "2026-10", tmp_path)
    summ, client = summarizer(settings, VALID)
    summarize_rollup("monthly", inputs, settings, summ)

    assert client.calls[0]["model"] == "claude-monthly-test"
    assert client.documents() == [
        ("daily", "2026-10-02", "## Tech\n- A lab released a model.\n"),
        ("weekly", "2026-W42", "## Tech\nA quiet week.\n"),
    ]
    assert "never more than 44" in client.calls[0]["messages"][0]["content"]


def test_too_many_words_is_retried_once(week, settings):
    inputs = load_inputs("weekly", "2026-W43", week)
    summ, client = summarizer(settings, words(30), VALID)
    body = summarize_rollup("weekly", inputs, settings, summ)

    assert (body.text, body.attempts, body.problems) == (VALID, 2, [])
    assert body.retried_for[0].startswith("The digest has 30 words, but at most 20")
    retry = client.calls[1]["messages"]
    assert retry[1] == {"role": "assistant", "content": words(30).strip()}
    assert "30 words" in retry[2]["content"]
    assert (body.input_tokens, body.output_tokens) == (200, 40)


def test_a_structure_problem_is_retried_once(week, settings):
    inputs = load_inputs("weekly", "2026-W43", week)
    summ, client = summarizer(settings, "# Weekly\n" + VALID, VALID)
    body = summarize_rollup("weekly", inputs, settings, summ)
    assert body.attempts == 2 and body.problems == []


def test_still_too_long_after_the_retry_is_kept_and_flagged(week, settings):
    inputs = load_inputs("weekly", "2026-W43", week)
    summ, client = summarizer(settings, words(30), words(25))
    body = summarize_rollup("weekly", inputs, settings, summ)

    assert (body.text, body.attempts, body.words) == (words(25), 2, 25)
    assert body.problems[0].startswith("The digest has 25 words, but at most 20")
    assert len(client.calls) == 2


# --- the run steps ------------------------------------------------------------------------------


def run_rollup(level, day, root, settings, *answers):
    config = Config(settings=settings, sources=[])
    summ, client = summarizer(settings, *answers)
    steps = RollupSteps(config, summarizer=summ, digests_dir=root).steps()
    log = io.StringIO()
    outcome = run(
        RunRequest(level, day, scheduled=False),
        config,
        steps=steps,
        digests_dir=root,
        now=datetime(2026, 10, 26, 6, tzinfo=UTC),
        log=log,
    )
    return outcome, client, log.getvalue()


def test_weekly_run_writes_the_digest(week, settings):
    outcome, client, log = run_rollup("weekly", date(2026, 10, 25), week, settings, VALID)

    assert outcome is Outcome.WRITTEN
    digest = read_digest("weekly", "2026-W43", week)
    assert digest.body == VALID
    assert digest.meta.model == "claude-rollup-test"
    assert digest.meta.inputs == ["2026-10-19", "2026-10-21"]
    assert "inputs: 2026-10-19, 2026-10-21" in log
    assert "1 answer(s)" in log


def test_monthly_run_writes_the_digest(tmp_path, settings):
    write(tmp_path, "weekly", "2026-W42", "## Tech\nA quiet week.\n")
    write(tmp_path, "daily", "2026-10-31", DAILY_BODY)
    outcome, _, _ = run_rollup("monthly", date(2026, 10, 31), tmp_path, settings, VALID)

    assert outcome is Outcome.WRITTEN
    assert read_digest("monthly", "2026-10", tmp_path).meta.inputs == ["2026-W42", "2026-10-31"]


def test_over_length_twice_is_written_with_a_warning(week, settings):
    outcome, _, log = run_rollup("weekly", date(2026, 10, 25), week, settings, words(30), words(25))
    assert outcome is Outcome.WRITTEN
    assert read_digest("weekly", "2026-W43", week).body == words(25)
    assert "warning: The digest has 25 words, but at most 20" in log


def test_no_inputs_does_not_call_claude(tmp_path, settings):
    outcome, client, _ = run_rollup("weekly", date(2026, 10, 25), tmp_path, settings)
    assert outcome is Outcome.NO_INPUTS
    assert client.calls == []


class FailingSummarizer:
    def summarize(self, level, task, documents, *, revision=None):
        raise SummarizerError("Claude API error: overloaded")


def test_claude_failure_fails_the_run_and_writes_nothing(week, settings):
    config = Config(settings=settings, sources=[])
    steps = RollupSteps(config, summarizer=FailingSummarizer(), digests_dir=week).steps()
    log = io.StringIO()
    outcome = run(
        RunRequest("weekly", date(2026, 10, 25), scheduled=False),
        config,
        steps=steps,
        digests_dir=week,
        log=log,
    )
    assert outcome is Outcome.FAILED
    assert "overloaded" in log.getvalue()
    assert not digest_path("weekly", "2026-W43", week).exists()


def test_a_missing_prompt_fails_the_run(week, settings, tmp_path_factory):
    config = Config(settings=settings, sources=[])
    summ, client = summarizer(settings, VALID)
    empty = tmp_path_factory.mktemp("prompts")
    steps = RollupSteps(config, summarizer=summ, digests_dir=week, prompts_dir=empty).steps()
    log = io.StringIO()
    outcome = run(
        RunRequest("weekly", date(2026, 10, 25), scheduled=False),
        config,
        steps=steps,
        digests_dir=week,
        log=log,
    )
    assert outcome is Outcome.FAILED
    assert "weekly.md: cannot read file" in log.getvalue()
    assert client.calls == []
