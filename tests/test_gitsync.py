"""Commit and push against real git repos in tmp_path: a bare remote and two clones."""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aggregator.gitsync import BOT_NAME, Committer, run_git  # the original, before conftest
from aggregator.pipeline import StepFailed
from aggregator.render import Digest, FrontMatter
from aggregator.store import write_digest

NOW = datetime(2026, 10, 15, 6, 3, 12, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch, tmp_path):
    # The user's global config (signing, hooks, default branch) must not affect the tests.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def clone(remote: Path, path: Path, name: str) -> Path:
    git(remote.parent, "clone", "--quiet", str(remote), str(path))
    git(path, "config", "user.name", name)
    git(path, "config", "user.email", f"{name}@example.com")
    return path


@pytest.fixture
def repos(tmp_path):
    """(remote, work, other): `work` is where the aggregator runs, `other` pushes concurrently."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(remote))
    seed = clone(remote, tmp_path / "seed", "seed")
    (seed / "README.md").write_text("readme\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--quiet", "-m", "initial")
    git(seed, "push", "--quiet", "origin", "main")
    work = clone(remote, tmp_path / "work", "owner")
    other = clone(remote, tmp_path / "other", "other")
    return remote, work, other


def digest(period="2026-10-15") -> Digest:
    meta = FrontMatter(level="daily", period=period, generated_at=NOW, model="m", inputs=["a"])
    return Digest(meta=meta, body="## Tech\n- x\n")


def write_outputs(work: Path, d: Digest | None = None, seen: str = '{"seen": {}}\n') -> Path:
    path = write_digest(d or digest(), work / "digests")
    (work / "state").mkdir(exist_ok=True)
    (work / "state" / "seen.json").write_text(seen)
    return path


def push_from_other(other: Path, name="other.txt", content="other\n") -> str:
    git(other, "pull", "--quiet", "--rebase", "origin", "main")
    (other / name).parent.mkdir(parents=True, exist_ok=True)
    (other / name).write_text(content)
    git(other, "add", name)
    git(other, "commit", "--quiet", "-m", f"other: {name}")
    git(other, "push", "--quiet", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def committer(work: Path, git_runner=run_git, env=None) -> tuple[Committer, io.StringIO]:
    log = io.StringIO()
    return Committer(root=work, git=git_runner, env=env or {}, log=log), log


def remote_head(remote: Path) -> str:
    return git(remote, "rev-parse", "main")


# --- commit -------------------------------------------------------------------------------------


def test_commits_only_digests_and_state_and_returns_the_pushed_sha(repos):
    remote, work, _ = repos
    path = write_outputs(work)
    (work / "notes.txt").write_text("unrelated, staged\n")
    git(work, "add", "notes.txt")
    (work / "README.md").write_text("unrelated, unstaged\n")
    c, log = committer(work)

    sha = c.commit(digest(), path)

    assert sha == remote_head(remote) == git(work, "rev-parse", "HEAD")
    files = git(work, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert files == ["digests/daily/2026/2026-10-15.md", "state/seen.json"]
    assert git(work, "log", "-1", "--format=%s %an") == "digest: daily 2026-10-15 owner"
    assert "A  notes.txt" in git(work, "status", "--short")  # still staged, not committed
    assert (work / "README.md").read_text() == "unrelated, unstaged\n"
    assert f"git: pushed {sha[:12]} to origin/main" in log.getvalue()


def test_in_actions_the_commit_is_made_as_the_bot(repos):
    _, work, _ = repos
    c, _ = committer(work, env={"GITHUB_ACTIONS": "true"})
    c.commit(digest(), write_outputs(work))
    assert git(work, "log", "-1", "--format=%an|%cn") == f"{BOT_NAME}|{BOT_NAME}"


def test_nothing_to_commit_returns_none(repos):
    _, work, _ = repos
    path = write_outputs(work)
    c, log = committer(work)
    c.commit(digest(), path)

    assert c.commit(digest(), path) is None  # e.g. --force with an identical digest
    assert "nothing to commit" in log.getvalue()


def test_a_missing_state_dir_is_fine(repos):
    remote, work, _ = repos
    path = write_digest(digest(), work / "digests")  # weekly etc. write no state
    assert committer(work)[0].commit(digest(), path) == remote_head(remote)


# --- rebase and retry ---------------------------------------------------------------------------


def test_rebases_onto_a_remote_that_moved_ahead(repos):
    remote, work, other = repos
    theirs = push_from_other(other)
    c, _ = committer(work)

    sha = c.commit(digest(), write_outputs(work))

    assert sha == remote_head(remote)
    assert git(work, "rev-parse", "HEAD~1") == theirs
    assert (work / "other.txt").is_file()


def test_a_simulated_conflict_is_rebased_and_pushed(repos):
    """Another run pushes between our rebase and our push: the push is rejected and retried."""
    remote, work, other = repos
    raced = []

    def racing_git(args, cwd):
        if args[0] == "push" and not raced:
            raced.append(push_from_other(other))
        return run_git(args, cwd)

    c, log = committer(work, racing_git)
    sha = c.commit(digest(), write_outputs(work))

    assert "push attempt 1 rejected" in log.getvalue()
    assert sha == remote_head(remote)
    assert git(work, "rev-parse", "HEAD~1") == raced[0]


def test_a_push_that_keeps_failing_fails_the_step(repos):
    _, work, other = repos

    def always_racing(args, cwd):
        if args[0] == "push":
            push_from_other(other, name=f"race-{len(list(other.glob('race-*')))}.txt")
        return run_git(args, cwd)

    c, log = committer(work, always_racing)
    with pytest.raises(StepFailed, match="push to origin/main failed after 3 attempts"):
        c.commit(digest(), write_outputs(work))
    assert log.getvalue().count("git: push attempt") == 3
    assert "[rejected]" in log.getvalue()


def test_a_rebase_conflict_is_aborted_and_fails_the_step(repos):
    _, work, other = repos
    push_from_other(other, name="state/seen.json", content='{"seen": {"x": 1}}\n')
    c, _ = committer(work)

    with pytest.raises(StepFailed, match="git pull --rebase failed"):
        c.commit(digest(), write_outputs(work, seen='{"seen": {"y": 2}}\n'))

    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()


def test_unstaged_changes_survive_the_rebase(repos):
    _, work, other = repos
    push_from_other(other)
    (work / "README.md").write_text("local edit\n")

    committer(work)[0].commit(digest(), write_outputs(work))

    assert (work / "README.md").read_text() == "local edit\n"


def test_a_detached_head_fails_the_step(repos):
    _, work, _ = repos
    git(work, "checkout", "--quiet", "--detach")
    with pytest.raises(StepFailed, match="detached HEAD"):
        committer(work)[0].commit(digest(), write_outputs(work))


def test_git_failures_are_step_failures(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(StepFailed, match="git rev-parse failed"):
        committer(not_a_repo)[0].commit(digest(), not_a_repo / "x.md")


def test_the_pipeline_releases_the_pushed_commit(repos):
    from datetime import date

    from aggregator.config import Config, Settings
    from aggregator.pipeline import Collected, Outcome, RunRequest, Steps, run

    remote, work, _ = repos
    released = []
    c, _ = committer(work)
    steps = Steps(
        collect=lambda req, cfg: Collected(["a-feed"]),
        commit=c.commit,
        release=lambda d, target: released.append(target),
    )
    config = Config(settings=Settings(categories=["Tech"]), sources=[])
    request = RunRequest(level="daily", day=date(2026, 10, 15), scheduled=False)

    outcome = run(request, config, steps=steps, digests_dir=work / "digests", log=io.StringIO())

    assert outcome is Outcome.WRITTEN
    assert released == [remote_head(remote)]
    assert "digests/daily/2026/2026-10-15.md" in git(remote, "ls-tree", "-r", "--name-only", "main")
