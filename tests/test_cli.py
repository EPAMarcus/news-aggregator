import subprocess
import sys
from datetime import date

import pytest

from aggregator import __version__
from aggregator.cli import default_steps, main
from aggregator.daily import DailySteps
from aggregator.gh import GhResult
from aggregator.pipeline import Body, Collected, StepFailed, Steps, skip_release, stub_collect
from aggregator.release import Releaser


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "aggregator" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aggregator", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "usage: aggregator" in result.stdout


# --- run ---------------------------------------------------------------------------------------

TODAY = date(2026, 10, 18)  # a Sunday


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("AGGREGATOR_CONFIG_DIR", raising=False)


def stub_steps(request, config):
    """The pipeline's stub steps for every level: no network, no Claude."""
    return Steps()


class RecordingAlerter:
    """Records alerts instead of opening issues; `fail` simulates a gh failure."""

    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.alerts = []

    def raise_alert(self, alert):
        self.alerts.append(alert)
        if self.fail:
            raise StepFailed(self.fail)


def cli(args, root, today=TODAY, make_steps=stub_steps, alerter=None):
    return main(
        ["run", *args],
        today=today,
        digests_dir=root,
        make_steps=make_steps,
        alerter=alerter or RecordingAlerter(),
    )


def test_run_dry_run_prints_the_digest_and_writes_nothing(tmp_path, env, capsys):
    assert cli(["--level", "daily", "--dry-run"], tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("---\nlevel: daily\nperiod: '2026-10-18'\n")
    assert "dry run" in captured.err
    assert list(tmp_path.iterdir()) == []


def test_run_writes_then_skips_the_second_time(tmp_path, env, capsys):
    assert cli(["--level", "weekly"], tmp_path) == 0
    path = tmp_path / "weekly" / "2026" / "2026-W42.md"
    before = path.read_bytes()

    assert cli(["--level", "weekly"], tmp_path) == 0
    assert "already exists" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_run_without_date_is_a_scheduled_run(tmp_path, env, capsys):
    assert cli(["--level", "monthly"], tmp_path) == 0  # 18 October is not a month end
    assert "not due" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_run_with_date_is_a_backfill(tmp_path, env):
    assert cli(["--level", "monthly", "--date", "2026-09-10"], tmp_path) == 0
    assert (tmp_path / "monthly" / "2026" / "2026-09.md").is_file()


def test_run_force_replaces(tmp_path, env, capsys):
    cli(["--level", "daily"], tmp_path)
    capsys.readouterr()
    assert cli(["--level", "daily", "--force"], tmp_path) == 0
    assert "wrote" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--level"),
        (["--level", "hourly"], "invalid choice"),
        (["--level", "daily", "--date", "2026-02-30"], "invalid date"),
        (["--level", "daily", "--date", "18.10.2026"], "invalid date"),
        (["--level", "daily", "--date", "2026-10-19"], "in the future"),
    ],
)
def test_run_rejects_bad_arguments(tmp_path, env, capsys, args, message):
    with pytest.raises(SystemExit) as exc:
        cli(args, tmp_path)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_run_reports_config_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGGREGATOR_CONFIG_DIR", str(tmp_path / "missing"))
    assert cli(["--level", "daily"], tmp_path) == 1
    assert "configuration error" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_run_without_inputs_exits_with_an_error(tmp_path, env, capsys):
    def no_inputs(request, config):
        return Steps(collect=lambda req, cfg: Collected([]))

    assert cli(["--level", "daily"], tmp_path, make_steps=no_inputs) == 1
    assert "no inputs" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_run_with_a_failed_step_exits_with_an_error(tmp_path, env, capsys):
    def failing(request, config):
        def summarize(req, cfg, collected):
            raise StepFailed("ANTHROPIC_API_KEY is not set")

        return Steps(collect=stub_collect, summarize=summarize)

    assert cli(["--level", "daily"], tmp_path, make_steps=failing) == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_default_steps_are_real_for_daily_only(env):
    from aggregator.config import load_config
    from aggregator.pipeline import RunRequest

    config = load_config()
    daily = default_steps(RunRequest("daily", TODAY), config)
    assert isinstance(daily.collect.__self__, DailySteps)
    assert isinstance(daily.release.__self__, Releaser)
    for level in ("weekly", "monthly", "yearly"):
        steps = default_steps(RunRequest(level, TODAY), config)
        assert steps == Steps() and steps.release is skip_release


def test_run_with_a_failed_release_exits_with_an_error(tmp_path, env, capsys):
    def failing_release(request, config):
        def release(digest, target):
            raise StepFailed("gh release create daily-2026-10-18 failed: HTTP 403")

        return Steps(release=release)

    assert cli(["--level", "daily"], tmp_path, make_steps=failing_release) == 1
    assert "release failed: gh release create" in capsys.readouterr().err
    assert (tmp_path / "daily" / "2026" / "2026-10-18.md").is_file()


# --- alerts (spec 7, architecture section 5) ------------------------------------------------------


def failing_summarize(message):
    def make(request, config):
        def summarize(req, cfg, collected):
            raise StepFailed(message)

        return Steps(collect=stub_collect, summarize=summarize)

    return make


def test_a_failed_run_raises_an_alert_with_the_error_and_the_log(tmp_path, env):
    alerter = RecordingAlerter()
    make = failing_summarize("Claude API error after 4 attempts: overloaded")

    assert cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter) == 1

    (alert,) = alerter.alerts
    assert (alert.level, alert.period, alert.outcome) == ("daily", "2026-10-18", "failed")
    assert alert.errors == [
        "daily digest 2026-10-18 failed, skipped: Claude API error after 4 attempts: overloaded"
    ]
    assert "Claude API error" in alert.log


def test_no_inputs_raises_an_alert(tmp_path, env):
    # Covers "all sources fail / no items" and "missing lower-level digests" (nothing to roll up).
    alerter = RecordingAlerter()
    for level in ("daily", "weekly"):
        make = lambda req, cfg: Steps(collect=lambda r, c: Collected([]))  # noqa: E731
        assert cli(["--level", level], tmp_path, make_steps=make, alerter=alerter) == 1
    assert [(a.level, a.outcome) for a in alerter.alerts] == [
        ("daily", "no-inputs"),
        ("weekly", "no-inputs"),
    ]
    assert alerter.alerts[0].errors == ["no inputs for daily digest 2026-10-18, skipped"]


def test_warnings_alone_raise_no_alert(tmp_path, env):
    # One failing source, or a daily digest truncated after the retry (architecture 3.4).
    def make(request, config):
        def summarize(req, cfg, collected):
            print("warning: source sap-news failed: timed out", file=sys.stderr)
            return Body("## Tech\n- x\n", "claude-test", problems=["truncated to 10 bullets"])

        return Steps(collect=stub_collect, summarize=summarize)

    alerter = RecordingAlerter()
    assert cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter) == 0
    assert alerter.alerts == []


def test_warnings_are_included_in_an_alert(tmp_path, env):
    def make(request, config):
        def summarize(req, cfg, collected):
            print("warning: source sap-news failed: timed out", file=sys.stderr)
            raise StepFailed("Claude API error")

        return Steps(collect=stub_collect, summarize=summarize)

    alerter = RecordingAlerter()
    cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter)
    assert alerter.alerts[0].warnings == ["source sap-news failed: timed out"]


def test_a_duplicate_trigger_raises_no_alert(tmp_path, env):
    alerter = RecordingAlerter()
    assert cli(["--level", "daily"], tmp_path, alerter=alerter) == 0
    assert cli(["--level", "daily"], tmp_path, alerter=alerter) == 0
    assert alerter.alerts == []


@pytest.mark.parametrize(
    ("step", "message"),
    [("commit", "push rejected after 3 attempts"), ("release", "HTTP 403")],
)
def test_a_failed_commit_or_release_raises_an_alert(tmp_path, env, step, message):
    def fail(*args):
        raise StepFailed(message)

    alerter = RecordingAlerter()
    make = lambda req, cfg: Steps(**{step: fail})  # noqa: E731
    assert cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter) == 1

    (alert,) = alerter.alerts
    assert alert.outcome == "publish-failed"
    assert alert.errors == [f"daily digest 2026-10-18 was written, but {step} failed: {message}"]


def test_a_config_error_raises_an_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("AGGREGATOR_CONFIG_DIR", str(tmp_path / "missing"))
    alerter = RecordingAlerter()

    assert cli(["--level", "daily"], tmp_path, alerter=alerter) == 1

    (alert,) = alerter.alerts
    assert alert.outcome == "config-error"
    assert "file not found" in alert.errors[0]


def test_an_unexpected_error_raises_an_alert_with_the_traceback(tmp_path, env, capsys):
    def make(request, config):
        def summarize(req, cfg, collected):
            raise KeyError("model")

        return Steps(collect=stub_collect, summarize=summarize)

    alerter = RecordingAlerter()
    assert cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter) == 1

    (alert,) = alerter.alerts
    assert alert.outcome == "crash"
    assert alert.errors == ["unexpected error: KeyError: 'model'"]
    assert "Traceback (most recent call last)" in alert.log
    assert "Traceback" in capsys.readouterr().err  # still printed for the workflow log


def test_a_failed_alert_still_exits_with_an_error(tmp_path, env, capsys):
    alerter = RecordingAlerter(fail="gh issue create failed: HTTP 403")
    make = failing_summarize("Claude API error")

    assert cli(["--level", "daily"], tmp_path, make_steps=make, alerter=alerter) == 1
    err = capsys.readouterr().err
    assert "the alert issue could not be created (gh issue create failed: HTTP 403)" in err
    assert "workflow-failure email is the only alert" in err


def test_a_failed_dry_run_raises_no_alert(tmp_path, env):
    alerter = RecordingAlerter()
    make = failing_summarize("Claude API error")
    assert cli(["--level", "daily", "--dry-run"], tmp_path, make_steps=make, alerter=alerter) == 1
    assert alerter.alerts == []


def test_the_default_alerter_is_used_when_none_is_given(tmp_path, env, monkeypatch, capsys):
    from aggregator import gh

    calls = []
    monkeypatch.setattr(gh, "run_gh", lambda args, stdin=None: calls.append(args) or GhResult(0))
    make = failing_summarize("Claude API error")
    assert (
        main(["run", "--level", "daily"], today=TODAY, digests_dir=tmp_path, make_steps=make) == 1
    )
    assert ["issue", "create"] in [c[:2] for c in calls]
