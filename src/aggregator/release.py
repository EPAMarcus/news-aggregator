"""Publish a digest as a GitHub release with the `gh` CLI (spec 6, architecture 3.7).

    releaser = Releaser()                    # gh reads GH_TOKEN (GITHUB_TOKEN in Actions)
    releaser.release(digest, target=sha)     # tag daily-2026-10-15, title [Digest] Daily 2026-10-15

The release body is the digest Markdown without front matter; GitHub renders it, and the owner is
notified through GitHub's release notifications. The tag points at `target`, the pushed commit
that contains the digest (#17); without a target, gh tags the head of the default branch.

The step is idempotent: if the release for the tag already exists, it is left alone. The pipeline
calls it again for a digest that already exists, so a failed release is repaired by rerunning.

Any `gh` failure raises `StepFailed`, which the pipeline turns into an error outcome (#16).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from aggregator.config import PROJECT_ROOT
from aggregator.pipeline import StepFailed
from aggregator.render import Digest

GH_TIMEOUT_SECONDS = 60.0
TITLE_PREFIX = "[Digest]"


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# Runs `gh` with the given arguments and optional stdin; tests pass a stub.
Gh = Callable[[Sequence[str], str | None], GhResult]


def run_gh(args: Sequence[str], stdin: str | None = None) -> GhResult:
    """Run `gh` in the project directory, so that it picks the repo from the git remote."""
    try:
        done = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise StepFailed("the gh CLI is not installed") from None
    except subprocess.TimeoutExpired:
        raise StepFailed(f"gh {args[0]} timed out after {GH_TIMEOUT_SECONDS:.0f}s") from None
    return GhResult(done.returncode, done.stdout, done.stderr)


def release_tag(digest: Digest) -> str:
    """'daily-2026-10-15', 'weekly-2026-W42', 'monthly-2026-10', 'yearly-2026'."""
    return f"{digest.meta.level}-{digest.meta.period}"


def release_title(digest: Digest) -> str:
    """'[Digest] Daily 2026-10-15', '[Digest] Weekly 2026-W42' and so on."""
    return f"{TITLE_PREFIX} {digest.meta.level.capitalize()} {digest.meta.period}"


@dataclass
class Releaser:
    gh: Gh = run_gh
    log: TextIO = field(default_factory=lambda: sys.stderr)

    def exists(self, tag: str) -> bool:
        # Any failure counts as "not there": a real problem (auth, network) then fails the create.
        return self.gh(["release", "view", tag, "--json", "tagName"], None).returncode == 0

    def release(self, digest: Digest, target: str | None = None) -> None:
        """The pipeline's release step (`pipeline.Release`). Raises `StepFailed`."""
        tag = release_tag(digest)
        if self.exists(tag):
            print(f"release {tag} already exists, left as it is", file=self.log)
            return
        args = ["release", "create", tag, "--title", release_title(digest), "--notes-file", "-"]
        if target is not None:
            args += ["--target", target]
        result = self.gh(args, digest.body)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
            raise StepFailed(f"gh release create {tag} failed: {detail}")
        print(f"released {tag}", file=self.log)
