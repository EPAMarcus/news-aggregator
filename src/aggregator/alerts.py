"""Alert issues for failed runs (spec 7, architecture 3.8).

    alerter = Alerter()
    alerter.raise_alert(alert)       # opens "[Alert] Daily 2026-10-15", or comments on it

An alert is an issue labelled `alert`, assigned to the owner so that GitHub notifies them, with
the level, period, errors and the run log. A repeated failure for the same digest comments on the
open alert issue with the same title instead of opening a new one. The run also exits non-zero,
so GitHub's workflow-failure email is sent too; if the issue cannot be created, that email is the
only alert.

The assignee is `ALERT_ASSIGNEE`, else the repository owner (`GITHUB_REPOSITORY_OWNER`, set in
Actions), else the logged-in `gh` user (`@me`, for local runs).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from aggregator import gh as gh_cli
from aggregator.gh import Gh
from aggregator.pipeline import StepFailed
from aggregator.schedule import Level

LABEL = "alert"
LABEL_COLOR = "B60205"
LABEL_DESCRIPTION = "A digest run failed"
TITLE_PREFIX = "[Alert]"
ASSIGNEE_ENV_VAR = "ALERT_ASSIGNEE"
LOG_MAX_LINES = 200  # issue bodies are limited to 65536 characters


@dataclass(frozen=True)
class Alert:
    level: Level
    period: str
    outcome: str  # e.g. "failed", "no-inputs", "config-error", "crash"
    errors: Sequence[str] = ()
    warnings: Sequence[str] = ()
    log: str = ""


def alert_title(level: Level, period: str) -> str:
    """'[Alert] Daily 2026-10-15', '[Alert] Weekly 2026-W42' and so on."""
    return f"{TITLE_PREFIX} {level.capitalize()} {period}"


def run_url(env: Mapping[str, str]) -> str | None:
    """The link to the workflow run, if running in GitHub Actions."""
    parts = [env.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    if not all(parts):
        return None
    server, repo, run_id = parts
    return f"{server}/{repo}/actions/runs/{run_id}"


def _fenced(text: str) -> str:
    # The fence must be longer than any run of backticks in the log.
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def render_alert(alert: Alert, url: str | None = None) -> str:
    """The Markdown body of an alert issue or comment."""
    lines = [
        f"**Level:** {alert.level}  ",
        f"**Period:** {alert.period}  ",
        f"**Outcome:** {alert.outcome}  ",
    ]
    if url:
        lines.append(f"**Run:** {url}  ")
    lines.append("")
    for heading, entries in (("Errors", alert.errors), ("Warnings", alert.warnings)):
        if entries:
            lines += [f"### {heading}", *(f"- {' '.join(e.split())}" for e in entries), ""]
    if alert.log.strip():
        log_lines = alert.log.rstrip().splitlines()
        shown = log_lines[-LOG_MAX_LINES:]
        summary = (
            "Run log" if len(shown) == len(log_lines) else f"Run log (last {len(shown)} lines)"
        )
        lines += [
            "<details>",
            f"<summary>{summary}</summary>",
            "",
            _fenced("\n".join(shown)),
            "",
            "</details>",
            "",
        ]
    lines.append(
        f"To retry: `gh workflow run daily.yml -f level={alert.level} -f date=<a day of "
        f"{alert.period}> -f dry_run=false`, or locally `aggregator run --level {alert.level} "
        f"--date <a day of {alert.period}>`. A digest that was written but not released is "
        "released by the rerun."
    )
    return "\n".join(lines) + "\n"


@dataclass
class Alerter:
    gh: Gh | None = None  # default: `gh.run_gh`
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    log: TextIO = field(default_factory=lambda: sys.stderr)

    def _gh(self, args: list[str], stdin: str | None = None) -> gh_cli.GhResult:
        return (self.gh or gh_cli.run_gh)(args, stdin)

    @property
    def assignee(self) -> str:
        return (
            self.env.get(ASSIGNEE_ENV_VAR, "").strip()
            or self.env.get("GITHUB_REPOSITORY_OWNER", "").strip()
            or "@me"
        )

    def open_issue(self, title: str) -> int | None:
        """The number of the open alert issue with this title, if any. A failed lookup counts as
        none: a duplicate issue is better than a lost alert."""
        result = self._gh(
            ["issue", "list", "--label", LABEL, "--state", "open", "--limit", "100",
             "--json", "number,title"]
        )  # fmt: skip
        if result.returncode != 0:
            return None
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        return next((i["number"] for i in issues if i.get("title") == title), None)

    def raise_alert(self, alert: Alert) -> None:
        """Open the alert issue, or comment on the open one. Raises `StepFailed`."""
        title = alert_title(alert.level, alert.period)
        body = render_alert(alert, run_url(self.env))
        number = self.open_issue(title)
        if number is not None:
            result = self._gh(["issue", "comment", str(number), "--body-file", "-"], body)
            if result.returncode != 0:
                raise StepFailed(f"gh issue comment {number} failed: {result.detail}")
            print(f"alert: commented on issue #{number} ({title})", file=self.log)
            return
        # The label may not exist yet; --force makes this a no-op when it does.
        self._gh(
            ["label", "create", LABEL, "--color", LABEL_COLOR,
             "--description", LABEL_DESCRIPTION, "--force"]
        )  # fmt: skip
        result = self._gh(
            ["issue", "create", "--title", title, "--label", LABEL,
             "--assignee", self.assignee, "--body-file", "-"],
            body,
        )  # fmt: skip
        if result.returncode != 0:
            raise StepFailed(f"gh issue create failed: {result.detail}")
        print(f"alert: opened {result.stdout.strip() or 'an issue'} ({title})", file=self.log)
