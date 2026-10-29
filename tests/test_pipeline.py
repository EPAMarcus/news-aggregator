import io
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aggregator.config import Config, Settings, load_config
from aggregator.pipeline import Body, Collected, Outcome, RunRequest, StepFailed, Steps, run
from aggregator.render import parse_digest
from aggregator.schedule import LEVELS
from aggregator.store import digest_exists, digest_path, read_digest

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 12, 31, 20, 5, tzinfo=UTC)
YEAR_END = date(2026, 12, 31)  # a Thursday: daily, monthly and yearly are due, weekly is not
SUNDAY = date(2026, 10, 18)
SHA = "0123abcd"


@pytest.fixture
def config() -> Config:
    return load_config(REPO_ROOT / "config", {})


class Recorder:
    """Steps that record their calls, to check which side effects happen."""

    def __init__(self, inputs=("a", "b"), problems=()):
        self.inputs = list(inputs)
        self.problems = list(problems)
        self.calls: list[str] = []

    def steps(self) -> Steps:
        return Steps(
            collect=lambda req, cfg: self._call("collect", Collected(self.inputs, data="data")),
            summarize=lambda req, cfg, collected: self._call(
                "summarize",
                Body(
                    f"body {req.period}\n",
                    getattr(cfg.settings.models, req.level),
                    notes=["a note"],
                    problems=self.problems,
                ),
            ),
            record=lambda req, collected: self._call("record"),
            commit=lambda digest, path: self._call("commit", SHA),
            release=lambda digest, target: self._call("release"),
        )

    def _call(self, name, result=None):
        self.calls.append(name)
        return result


def do_run(request, config, root, steps=None):
    out, log = io.StringIO(), io.StringIO()
    outcome = run(request, config, steps=steps, digests_dir=root, now=NOW, out=out, log=log)
    return outcome, out.getvalue(), log.getvalue()


def files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("level", LEVELS)
def test_dry_run_has_no_side_effects_for_every_level(tmp_path, config, level):
    rec = Recorder()
    request = RunRequest(level=level, day=YEAR_END, scheduled=False, dry_run=True)

    outcome, out, log = do_run(request, config, tmp_path, rec.steps())

    assert outcome is Outcome.DRY_RUN
    assert files(tmp_path) == []
    assert rec.calls == ["collect", "summarize"]  # no record, commit or release
    assert "dry run: would write" in log
    digest = parse_digest(out)
    assert (digest.meta.level, digest.meta.period) == (level, request.period)
    assert digest.meta.model == getattr(config.settings.models, level)
    assert digest.meta.generated_at == NOW


@pytest.mark.parametrize("level", LEVELS)
def test_dry_run_with_the_default_stub_steps(tmp_path, config, level):
    request = RunRequest(level=level, day=YEAR_END, scheduled=False, dry_run=True)
    outcome, out, _ = do_run(request, config, tmp_path)
    assert outcome is Outcome.DRY_RUN
    assert files(tmp_path) == []
    body = parse_digest(out).body
    assert all(f"## {c}" in body for c in config.settings.categories)


@pytest.mark.parametrize("level", LEVELS)
def test_second_run_for_the_same_period_changes_nothing(tmp_path, config, level):
    request = RunRequest(level=level, day=YEAR_END, scheduled=False)
    first = Recorder()
    assert do_run(request, config, tmp_path, first.steps())[0] is Outcome.WRITTEN
    assert first.calls == ["collect", "summarize", "record", "commit", "release"]
    path = digest_path(level, request.period, tmp_path)
    before = path.read_bytes()

    second = Recorder()
    outcome, _, log = do_run(request, config, tmp_path, second.steps())

    assert outcome is Outcome.EXISTS
    assert "already exists" in log
    assert second.calls == ["release"]  # repairs a missing release; a no-op if it exists
    assert path.read_bytes() == before
    assert files(tmp_path) == [path]


def test_another_day_of_the_same_period_is_also_a_duplicate(tmp_path, config):
    run_on = lambda day: do_run(RunRequest("weekly", day, scheduled=False), config, tmp_path)[0]  # noqa: E731
    assert run_on(date(2026, 10, 12)) is Outcome.WRITTEN  # Monday of 2026-W42
    assert run_on(SUNDAY) is Outcome.EXISTS


def test_force_replaces_an_existing_digest(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path, Recorder(inputs=["old"]).steps())

    rec = Recorder(inputs=["new"])
    outcome, _, _ = do_run(replace(request, force=True), config, tmp_path, rec.steps())

    assert outcome is Outcome.WRITTEN
    assert rec.calls == ["collect", "summarize", "record", "commit", "release"]
    assert read_digest("daily", "2026-12-31", tmp_path).meta.inputs == ["new"]


def test_dry_run_with_force_still_writes_nothing(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path)
    before = files(tmp_path)[0].read_bytes()

    outcome, _, _ = do_run(replace(request, force=True, dry_run=True), config, tmp_path)

    assert outcome is Outcome.DRY_RUN
    assert files(tmp_path)[0].read_bytes() == before


@pytest.mark.parametrize(
    ("level", "day", "due"),
    [
        ("daily", date(2026, 10, 14), True),
        ("weekly", date(2026, 10, 14), False),
        ("weekly", SUNDAY, True),
        ("monthly", date(2026, 10, 30), False),
        ("monthly", date(2026, 10, 31), True),
        ("yearly", date(2026, 11, 30), False),
        ("yearly", YEAR_END, True),
    ],
)
def test_scheduled_runs_only_produce_due_digests(tmp_path, config, level, day, due):
    rec = Recorder()
    outcome, _, log = do_run(RunRequest(level=level, day=day), config, tmp_path, rec.steps())
    if due:
        assert outcome is Outcome.WRITTEN
    else:
        assert outcome is Outcome.NOT_DUE
        assert "not due" in log
        assert rec.calls == []
        assert files(tmp_path) == []


def test_manual_runs_skip_the_due_check(tmp_path, config):
    request = RunRequest(level="weekly", day=date(2026, 10, 14), scheduled=False)
    assert do_run(request, config, tmp_path)[0] is Outcome.WRITTEN
    assert digest_exists("weekly", "2026-W42", tmp_path)


def test_no_inputs_skips_the_digest(tmp_path, config):
    rec = Recorder(inputs=[])
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    outcome, _, log = do_run(request, config, tmp_path, rec.steps())
    assert outcome is Outcome.NO_INPUTS
    assert "no inputs" in log
    assert rec.calls == ["collect"]
    assert files(tmp_path) == []


def test_written_digest_records_inputs_and_model(tmp_path):
    settings = Settings(categories=["Tech"], models={"monthly": "claude-test"})
    config = Config(settings=settings, sources=[])
    request = RunRequest(level="monthly", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path, Recorder(inputs=["2026-W50", "2026-12-14"]).steps())
    meta = read_digest("monthly", "2026-12", tmp_path).meta
    assert meta.inputs == ["2026-W50", "2026-12-14"]
    assert meta.model == "claude-test"


def test_a_failed_step_skips_the_digest(tmp_path, config):
    def fail(req, cfg, collected):
        raise StepFailed("Claude API error after 4 attempts")

    rec = Recorder()
    steps = replace(rec.steps(), summarize=fail)
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)

    outcome, _, log = do_run(request, config, tmp_path, steps)

    assert outcome is Outcome.FAILED and outcome.is_error
    assert "error: daily digest 2026-12-31 failed, skipped: Claude API error" in log
    assert rec.calls == ["collect"]  # nothing recorded, committed or sent
    assert files(tmp_path) == []


def test_a_failing_collect_is_also_a_failed_run(tmp_path, config):
    def fail(req, cfg):
        raise StepFailed("seen.json is broken")

    steps = replace(Recorder().steps(), collect=fail)
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    assert do_run(request, config, tmp_path, steps)[0] is Outcome.FAILED


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (Outcome.WRITTEN, False),
        (Outcome.DRY_RUN, False),
        (Outcome.NOT_DUE, False),
        (Outcome.EXISTS, False),
        (Outcome.NO_INPUTS, True),
        (Outcome.FAILED, True),
        (Outcome.PUBLISH_FAILED, True),
    ],
)
def test_error_outcomes(outcome, error):
    assert outcome.is_error is error


# --- release ------------------------------------------------------------------------------------


def test_the_release_step_gets_the_written_digest_and_the_pushed_commit(tmp_path, config):
    released = []
    steps = replace(Recorder().steps(), release=lambda d, target: released.append((d, target)))
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)

    do_run(request, config, tmp_path, steps)

    ((digest, target),) = released
    assert target == SHA
    assert read_digest("daily", "2026-12-31", tmp_path) == digest


def test_a_failed_release_keeps_the_digest_and_is_an_error(tmp_path, config):
    def fail(digest, target):
        raise StepFailed("gh release create daily-2026-12-31 failed: HTTP 403")

    rec = Recorder()
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)

    outcome, _, log = do_run(request, config, tmp_path, replace(rec.steps(), release=fail))

    assert outcome is Outcome.PUBLISH_FAILED
    assert "was written, but release failed: gh release create" in log
    assert rec.calls == ["collect", "summarize", "record", "commit"]
    assert digest_exists("daily", "2026-12-31", tmp_path)


def test_a_failed_commit_skips_the_release(tmp_path, config):
    def fail(digest, path):
        raise StepFailed("push rejected")

    rec = Recorder()
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)

    outcome, _, log = do_run(request, config, tmp_path, replace(rec.steps(), commit=fail))

    assert outcome is Outcome.PUBLISH_FAILED
    assert "was written, but commit failed: push rejected" in log
    assert rec.calls == ["collect", "summarize", "record"]


def test_a_rerun_releases_an_existing_digest(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path)  # written, but not released (the stub steps)

    released = []
    rec = Recorder()
    steps = replace(rec.steps(), release=lambda d, target: released.append((d, target)))
    outcome, _, _ = do_run(request, config, tmp_path, steps)

    assert outcome is Outcome.EXISTS
    ((digest, target),) = released
    assert digest == read_digest("daily", "2026-12-31", tmp_path)
    assert target is None  # already pushed: tag the head of the default branch
    assert rec.calls == []  # nothing collected, summarized or committed


def test_a_failed_release_on_rerun_is_an_error(tmp_path, config):
    def fail(digest, target):
        raise StepFailed("HTTP 502")

    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path)
    outcome, _, log = do_run(request, config, tmp_path, Steps(release=fail))

    assert outcome is Outcome.PUBLISH_FAILED
    assert "error: release of daily digest 2026-12-31 failed: HTTP 502" in log


def test_an_unreadable_existing_digest_is_a_failed_release(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path)
    digest_path("daily", "2026-12-31", tmp_path).write_text("no front matter\n")

    outcome, _, log = do_run(request, config, tmp_path, Recorder().steps())

    assert outcome is Outcome.PUBLISH_FAILED
    assert "missing front matter" in log


def test_a_dry_run_of_an_existing_digest_releases_nothing(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    do_run(request, config, tmp_path)

    rec = Recorder()
    outcome, _, _ = do_run(replace(request, dry_run=True), config, tmp_path, rec.steps())

    assert outcome is Outcome.EXISTS
    assert rec.calls == []


def test_notes_and_problems_are_logged(tmp_path, config):
    rec = Recorder(problems=["truncated to 10 bullets"])
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    outcome, _, log = do_run(request, config, tmp_path, rec.steps())
    assert outcome is Outcome.WRITTEN  # problems are flagged, not fatal
    assert "a note" in log
    assert "warning: truncated to 10 bullets" in log


def test_record_runs_after_the_write(tmp_path, config):
    request = RunRequest(level="daily", day=YEAR_END, scheduled=False)
    seen = []

    def record(req, collected):
        seen.append((digest_exists("daily", req.period, tmp_path), collected.data))

    steps = replace(Recorder().steps(), record=record)
    do_run(request, config, tmp_path, steps)
    assert seen == [(True, "data")]
