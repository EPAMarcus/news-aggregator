"""Run the GitHub CLI (`gh`) for releases (#15) and alert issues (#16).

`gh` authenticates with `GH_TOKEN` (the workflow's `GITHUB_TOKEN` in Actions) or a local
`gh auth login`, and picks the repo from the git remote of the project directory.

Callers look up `run_gh` on this module at call time, so tests replace it in one place.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from aggregator.config import PROJECT_ROOT
from aggregator.pipeline import StepFailed

GH_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def detail(self) -> str:
        """The error output for a message, or the exit code if there is none."""
        return (self.stderr or self.stdout).strip() or f"exit code {self.returncode}"


# Runs `gh` with the given arguments and optional stdin; tests pass a stub.
Gh = Callable[[Sequence[str], str | None], GhResult]


def run_gh(args: Sequence[str], stdin: str | None = None) -> GhResult:
    """Run `gh` in the project directory. Raises `StepFailed` if it is missing or hangs."""
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
