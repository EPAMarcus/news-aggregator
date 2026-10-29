"""Commit and push the digest and state files (architecture 3.6).

    committer = Committer()
    sha = committer.commit(digest, path)   # "digest: daily 2026-10-15", pushed; the release target

Only `digests/` and `state/` are committed, whatever else is staged or changed in the working
tree. In GitHub Actions (`GITHUB_ACTIONS=true`) the commit is made as `github-actions[bot]`;
locally, the git identity of the user is used. `actions/checkout` leaves `GITHUB_TOKEN` in the git
config, so the push needs no other credentials (`contents: write`).

The push goes to the current branch on `origin`, after `git pull --rebase --autostash`. A rejected
push (another run pushed first) is retried with a new rebase. A rebase conflict is aborted and
fails the step; the shared concurrency group of the workflows makes that unlikely.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from aggregator.config import PROJECT_ROOT
from aggregator.gh import GhResult as GitResult
from aggregator.pipeline import StepFailed
from aggregator.render import Digest

GIT_TIMEOUT_SECONDS = 120.0
PUSH_ATTEMPTS = 3
REMOTE = "origin"
COMMITTED_DIRS = ("digests", "state")
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

# Runs `git` with the given arguments in the given directory; tests pass a wrapper.
Git = Callable[[Sequence[str], Path], GitResult]


def run_git(args: Sequence[str], cwd: Path) -> GitResult:
    try:
        done = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise StepFailed("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise StepFailed(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS:.0f}s") from None
    return GitResult(done.returncode, done.stdout, done.stderr)


def commit_message(digest: Digest) -> str:
    return f"digest: {digest.meta.level} {digest.meta.period}"


@dataclass
class Committer:
    root: Path = PROJECT_ROOT
    dirs: Sequence[str] = COMMITTED_DIRS
    git: Git | None = None  # default: `gitsync.run_git`
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    log: TextIO = field(default_factory=lambda: sys.stderr)
    attempts: int = PUSH_ATTEMPTS

    def _git(self, *args: str) -> GitResult:
        return (self.git or run_git)(list(args), self.root)  # looked up at call time

    def _ok(self, *args: str) -> str:
        result = self._git(*args)
        if result.returncode != 0:
            raise StepFailed(f"git {args[0]} failed: {result.detail}")
        return result.stdout.strip()

    def _identity(self) -> list[str]:
        if self.env.get("GITHUB_ACTIONS") == "true":
            return ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
        return []

    def commit(self, digest: Digest, path: Path) -> str | None:
        """The pipeline's commit step (`pipeline.Commit`). Returns the SHA of the pushed commit,
        or None if there was nothing to commit. Raises `StepFailed`."""
        branch = self._ok("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            raise StepFailed("git: detached HEAD, no branch to push to")
        dirs = [d for d in self.dirs if (self.root / d).exists()]
        if not dirs:
            raise StepFailed(f"nothing to commit: none of {', '.join(self.dirs)} exists")
        self._ok("add", "--all", "--", *dirs)
        if self._git("diff", "--cached", "--quiet", "--", *dirs).returncode == 0:
            print("git: nothing to commit", file=self.log)
            return None
        # The pathspec commits only these directories, even if other changes are staged.
        self._ok(*self._identity(), "commit", "--quiet", "-m", commit_message(digest), "--", *dirs)

        for attempt in range(1, self.attempts + 1):
            pulled = self._git("pull", "--rebase", "--autostash", "--quiet", REMOTE, branch)
            if pulled.returncode != 0:
                self._git("rebase", "--abort")
                raise StepFailed(f"git pull --rebase failed: {pulled.detail}")
            pushed = self._git("push", "--quiet", REMOTE, f"HEAD:{branch}")
            if pushed.returncode == 0:
                sha = self._ok("rev-parse", "HEAD")
                print(f"git: pushed {sha[:12]} to {REMOTE}/{branch}", file=self.log)
                return sha
            lines = pushed.detail.splitlines()
            reason = next((line for line in lines if "[rejected]" in line), lines[0]).strip()
            print(f"git: push attempt {attempt} rejected: {reason}", file=self.log)
        raise StepFailed(f"git push to {REMOTE}/{branch} failed after {self.attempts} attempts")
