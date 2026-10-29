"""Publish a digest as a GitHub release with the `gh` CLI (spec 6, architecture 3.7).

    releaser = Releaser()                    # gh reads GH_TOKEN (GITHUB_TOKEN in Actions)
    releaser.release(digest, target=sha)     # tag daily-2026-10-15, title [Digest] Daily 2026-10-15

The release body is the digest Markdown without front matter; GitHub renders it, and the owner is
notified through GitHub's release notifications. The tag points at `target`, the pushed commit
that contains the digest (`aggregator.gitsync`); without a target, gh tags the head of the
default branch.

The step is idempotent: if the release for the tag already exists, it is left alone. The pipeline
calls it again for a digest that already exists, so a failed release is repaired by rerunning.

Any `gh` failure raises `StepFailed`, which the pipeline turns into an error outcome (#16).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TextIO

from aggregator import gh as gh_cli
from aggregator.gh import Gh
from aggregator.pipeline import StepFailed
from aggregator.render import Digest

TITLE_PREFIX = "[Digest]"


def release_tag(digest: Digest) -> str:
    """'daily-2026-10-15', 'weekly-2026-W42', 'monthly-2026-10', 'yearly-2026'."""
    return f"{digest.meta.level}-{digest.meta.period}"


def release_title(digest: Digest) -> str:
    """'[Digest] Daily 2026-10-15', '[Digest] Weekly 2026-W42' and so on."""
    return f"{TITLE_PREFIX} {digest.meta.level.capitalize()} {digest.meta.period}"


@dataclass
class Releaser:
    gh: Gh | None = None  # default: `gh.run_gh`
    log: TextIO = field(default_factory=lambda: sys.stderr)

    def _gh(self, args: list[str], stdin: str | None = None) -> gh_cli.GhResult:
        return (self.gh or gh_cli.run_gh)(args, stdin)

    def exists(self, tag: str) -> bool:
        # Any failure counts as "not there": a real problem (auth, network) then fails the create.
        return self._gh(["release", "view", tag, "--json", "tagName"]).returncode == 0

    def release(self, digest: Digest, target: str | None = None) -> None:
        """The pipeline's release step (`pipeline.Release`). Raises `StepFailed`."""
        tag = release_tag(digest)
        if self.exists(tag):
            print(f"release {tag} already exists, left as it is", file=self.log)
            return
        args = ["release", "create", tag, "--title", release_title(digest), "--notes-file", "-"]
        if target is not None:
            args += ["--target", target]
        result = self._gh(args, digest.body)
        if result.returncode != 0:
            raise StepFailed(f"gh release create {tag} failed: {result.detail}")
        print(f"released {tag}", file=self.log)
