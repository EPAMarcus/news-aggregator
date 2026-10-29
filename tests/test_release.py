"""The GitHub release step: tag, title, body and target, against a stubbed `gh`."""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime

import pytest

from aggregator import release as release_module
from aggregator.pipeline import StepFailed
from aggregator.release import GhResult, Releaser, release_tag, release_title, run_gh
from aggregator.render import Digest, FrontMatter

NOW = datetime(2026, 10, 15, 6, 3, 12, tzinfo=UTC)
BODY = "## Tech\n- A lab released a **new model**.\n\n## Sources\n- Hacker News\n"
SHA = "0123abcd"


def digest(level="daily", period="2026-10-15", body=BODY) -> Digest:
    meta = FrontMatter(
        level=level, period=period, generated_at=NOW, model="claude-test", inputs=["hn"]
    )
    return Digest(meta=meta, body=body)


class FakeGh:
    """A stub for the gh CLI: records the calls and answers from a set of existing tags."""

    def __init__(self, existing=(), create_result: GhResult | None = None):
        self.existing = set(existing)
        self.create_result = create_result or GhResult(0, "https://github.com/o/r/releases/x\n")
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin=None) -> GhResult:
        args = list(args)
        self.calls.append((args, stdin))
        if args[:2] == ["release", "view"]:
            found = args[2] in self.existing
            return GhResult(0, "{}") if found else GhResult(1, stderr="release not found\n")
        if args[:2] == ["release", "create"]:
            return self.create_result
        raise AssertionError(f"unexpected gh call: {args}")

    @property
    def creates(self):
        return [(args, stdin) for args, stdin in self.calls if args[:2] == ["release", "create"]]


def releaser(gh: FakeGh) -> tuple[Releaser, io.StringIO]:
    log = io.StringIO()
    return Releaser(gh=gh, log=log), log


# --- tag and title ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "period", "tag", "title"),
    [
        ("daily", "2026-10-15", "daily-2026-10-15", "[Digest] Daily 2026-10-15"),
        ("weekly", "2026-W42", "weekly-2026-W42", "[Digest] Weekly 2026-W42"),
        ("monthly", "2026-10", "monthly-2026-10", "[Digest] Monthly 2026-10"),
        ("yearly", "2026", "yearly-2026", "[Digest] Yearly 2026"),
    ],
)
def test_tag_and_title_format(level, period, tag, title):
    d = digest(level, period)
    assert release_tag(d) == tag
    assert release_title(d) == title


# --- release ------------------------------------------------------------------------------------


def test_creates_the_release_with_tag_title_body_and_target():
    gh = FakeGh()
    rel, log = releaser(gh)

    rel.release(digest(), SHA)

    ((args, stdin),) = gh.creates
    assert args == [
        "release", "create", "daily-2026-10-15",
        "--title", "[Digest] Daily 2026-10-15",
        "--notes-file", "-",
        "--target", SHA,
    ]  # fmt: skip
    assert stdin == BODY
    assert "released daily-2026-10-15" in log.getvalue()


def test_the_body_has_no_front_matter():
    gh = FakeGh()
    releaser(gh)[0].release(digest(), SHA)
    ((_, stdin),) = gh.creates
    assert not stdin.startswith("---")
    assert "claude-test" not in stdin and "generated_at" not in stdin


def test_without_a_target_gh_picks_the_default_branch():
    gh = FakeGh()
    releaser(gh)[0].release(digest(), None)
    ((args, _),) = gh.creates
    assert "--target" not in args


def test_an_existing_release_is_not_touched():
    gh = FakeGh(existing={"daily-2026-10-15"})
    rel, log = releaser(gh)

    rel.release(digest(), SHA)

    assert gh.calls == [(["release", "view", "daily-2026-10-15", "--json", "tagName"], None)]
    assert "already exists" in log.getvalue()


def test_a_failed_create_raises_step_failed_with_the_gh_message():
    gh = FakeGh(create_result=GhResult(1, stderr="HTTP 403: Resource not accessible\n"))
    with pytest.raises(StepFailed, match="gh release create daily-2026-10-15 failed: HTTP 403"):
        releaser(gh)[0].release(digest(), SHA)


def test_a_failed_create_without_output_reports_the_exit_code():
    gh = FakeGh(create_result=GhResult(4))
    with pytest.raises(StepFailed, match="exit code 4"):
        releaser(gh)[0].release(digest(), SHA)


# --- running gh ---------------------------------------------------------------------------------


def test_run_gh_passes_args_and_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, "out", "err")

    monkeypatch.setattr(release_module.subprocess, "run", fake_run)
    assert run_gh(["release", "view", "x"], "notes") == GhResult(0, "out", "err")
    assert seen["cmd"] == ["gh", "release", "view", "x"]
    assert seen["input"] == "notes"
    assert seen["cwd"] == release_module.PROJECT_ROOT


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError("gh"), "gh CLI is not installed"),
        (subprocess.TimeoutExpired("gh", 60), "timed out"),
    ],
)
def test_run_gh_failures_are_step_failures(monkeypatch, error, message):
    def fake_run(cmd, **kwargs):
        raise error

    monkeypatch.setattr(release_module.subprocess, "run", fake_run)
    with pytest.raises(StepFailed, match=message):
        run_gh(["release", "view", "x"])
