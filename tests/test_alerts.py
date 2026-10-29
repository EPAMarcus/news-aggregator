"""Alert issues: title, body, open-or-comment, assignee, against a stubbed `gh`."""

from __future__ import annotations

import io
import json

import pytest

from aggregator.alerts import Alert, Alerter, alert_title, render_alert, run_url
from aggregator.gh import GhResult
from aggregator.pipeline import StepFailed

ALERT = Alert(
    level="daily",
    period="2026-10-15",
    outcome="failed",
    errors=["daily digest 2026-10-15 failed, skipped: Claude API error"],
    warnings=["source sap-news failed: timed out"],
    log="warning: source sap-news failed: timed out\nerror: daily digest 2026-10-15 failed\n",
)
ACTIONS_ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "owner/news-aggregator",
    "GITHUB_RUN_ID": "123",
    "GITHUB_REPOSITORY_OWNER": "owner",
}


class FakeGh:
    """A stub for the gh CLI that knows a list of open alert issues."""

    def __init__(self, open_issues=(), fail: str | None = None, list_result=None):
        self.open_issues = list(open_issues)
        self.fail = fail  # "create" or "comment"
        self.list_result = list_result
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin=None) -> GhResult:
        args = list(args)
        self.calls.append((args, stdin))
        command = args[1] if args[0] == "issue" else args[0]
        if command == "list":
            return self.list_result or GhResult(0, json.dumps(self.open_issues))
        if command == "label":
            return GhResult(0)
        if command == self.fail:
            return GhResult(1, stderr="HTTP 403: Resource not accessible by integration\n")
        if command == "create":
            return GhResult(0, "https://github.com/owner/news-aggregator/issues/7\n")
        if command == "comment":
            return GhResult(0, "https://github.com/owner/news-aggregator/issues/7#c1\n")
        raise AssertionError(f"unexpected gh call: {args}")

    def commands(self):
        return [" ".join(args[:2]) for args, _ in self.calls]

    def call(self, prefix):
        return next((a, s) for a, s in self.calls if " ".join(a[:2]) == prefix)


def alerter(gh: FakeGh, env=None) -> tuple[Alerter, io.StringIO]:
    log = io.StringIO()
    return Alerter(gh=gh, env=ACTIONS_ENV if env is None else env, log=log), log


# --- title and body -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "period", "title"),
    [
        ("daily", "2026-10-15", "[Alert] Daily 2026-10-15"),
        ("weekly", "2026-W42", "[Alert] Weekly 2026-W42"),
        ("yearly", "2026", "[Alert] Yearly 2026"),
    ],
)
def test_title_format(level, period, title):
    assert alert_title(level, period) == title


def test_run_url_only_in_actions():
    assert run_url(ACTIONS_ENV) == "https://github.com/owner/news-aggregator/actions/runs/123"
    assert run_url({}) is None


def test_body_has_level_period_outcome_errors_warnings_and_log():
    body = render_alert(ALERT, "https://example.com/run")
    assert "**Level:** daily" in body
    assert "**Period:** 2026-10-15" in body
    assert "**Outcome:** failed" in body
    assert "**Run:** https://example.com/run" in body
    assert "### Errors\n- daily digest 2026-10-15 failed, skipped: Claude API error" in body
    assert "### Warnings\n- source sap-news failed: timed out" in body
    assert "<summary>Run log</summary>" in body
    assert "```text\nwarning: source sap-news failed" in body
    assert "aggregator run --level daily" in body


def test_body_without_run_url_warnings_or_log():
    body = render_alert(Alert("weekly", "2026-W42", "no-inputs", errors=["no inputs"]))
    assert "**Run:**" not in body and "### Warnings" not in body and "<details>" not in body


def test_multi_line_errors_stay_one_bullet():
    body = render_alert(Alert("daily", "2026-10-15", "config-error", errors=["a\n  b"]))
    assert "- a b\n" in body


def test_a_long_log_is_cut_to_its_last_lines():
    log = "\n".join(f"line {n}" for n in range(500))
    body = render_alert(Alert("daily", "2026-10-15", "crash", log=log))
    assert "Run log (last 200 lines)" in body
    assert "line 499" in body and "line 299\n" not in body


def test_backticks_in_the_log_do_not_break_the_fence():
    body = render_alert(Alert("daily", "2026-10-15", "crash", log="a ``` b\n"))
    assert "````text\na ``` b\n````" in body


# --- open or comment ----------------------------------------------------------------------------


def test_opens_an_assigned_labelled_issue():
    gh = FakeGh()
    a, log = alerter(gh)

    a.raise_alert(ALERT)

    assert gh.commands() == ["issue list", "label create", "issue create"]
    args, body = gh.call("issue create")
    assert args == [
        "issue", "create", "--title", "[Alert] Daily 2026-10-15", "--label", "alert",
        "--assignee", "owner", "--body-file", "-",
    ]  # fmt: skip
    assert body == render_alert(ALERT, run_url(ACTIONS_ENV))
    assert "alert: opened https://github.com/owner/news-aggregator/issues/7" in log.getvalue()


def test_a_repeated_failure_comments_on_the_open_issue():
    gh = FakeGh(
        open_issues=[
            {"number": 3, "title": "[Alert] Daily 2026-10-14"},
            {"number": 7, "title": "[Alert] Daily 2026-10-15"},
        ]
    )
    a, log = alerter(gh)

    a.raise_alert(ALERT)

    assert gh.commands() == ["issue list", "issue comment"]
    args, body = gh.call("issue comment")
    assert args == ["issue", "comment", "7", "--body-file", "-"]
    assert "Claude API error" in body
    assert "alert: commented on issue #7" in log.getvalue()


def test_a_failed_lookup_still_opens_an_issue():
    gh = FakeGh(list_result=GhResult(1, stderr="HTTP 502"))
    alerter(gh)[0].raise_alert(ALERT)
    assert "issue create" in gh.commands()


@pytest.mark.parametrize("fail", ["create", "comment"])
def test_a_gh_failure_raises_step_failed(fail):
    open_issues = [{"number": 7, "title": "[Alert] Daily 2026-10-15"}] if fail == "comment" else []
    gh = FakeGh(open_issues=open_issues, fail=fail)
    with pytest.raises(StepFailed, match=f"gh issue {fail}.* failed: HTTP 403"):
        alerter(gh)[0].raise_alert(ALERT)


@pytest.mark.parametrize(
    ("env", "assignee"),
    [
        ({"ALERT_ASSIGNEE": "someone", **ACTIONS_ENV}, "someone"),
        (ACTIONS_ENV, "owner"),
        ({}, "@me"),
    ],
)
def test_assignee(env, assignee):
    assert alerter(FakeGh(), env)[0].assignee == assignee
