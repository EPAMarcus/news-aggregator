import subprocess

import pytest

from aggregator import gh
from aggregator.gh import GhResult, run_gh  # the original, imported before conftest patches it
from aggregator.pipeline import StepFailed


def test_run_gh_passes_args_and_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, "out", "err")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert run_gh(["release", "view", "x"], "notes") == GhResult(0, "out", "err")
    assert seen["cmd"] == ["gh", "release", "view", "x"]
    assert seen["input"] == "notes"
    assert seen["cwd"] == gh.PROJECT_ROOT


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

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    with pytest.raises(StepFailed, match=message):
        run_gh(["release", "view", "x"])


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (GhResult(1, "out", "HTTP 403\n"), "HTTP 403"),
        (GhResult(1, "only stdout\n"), "only stdout"),
        (GhResult(4), "exit code 4"),
    ],
)
def test_detail(result, detail):
    assert result.detail == detail


def test_the_real_gh_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="a test called the real gh"):
        gh.run_gh(["issue", "list"])
