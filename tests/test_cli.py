import subprocess
import sys
from datetime import date

import pytest

from aggregator import __version__
from aggregator.cli import default_steps, main
from aggregator.daily import DailySteps
from aggregator.pipeline import Collected, StepFailed, Steps, stub_collect


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
    monkeypatch.setenv("RECIPIENT_EMAIL", "owner@example.com")
    monkeypatch.delenv("AGGREGATOR_CONFIG_DIR", raising=False)


def stub_steps(request, config):
    """The pipeline's stub steps for every level: no network, no Claude."""
    return Steps()


def cli(args, root, today=TODAY, make_steps=stub_steps):
    return main(["run", *args], today=today, digests_dir=root, make_steps=make_steps)


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
    monkeypatch.delenv("RECIPIENT_EMAIL", raising=False)
    assert cli(["--level", "daily"], tmp_path) == 1
    assert "RECIPIENT_EMAIL" in capsys.readouterr().err
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
    for level in ("weekly", "monthly", "yearly"):
        assert default_steps(RunRequest(level, TODAY), config) == Steps()
