import pytest

from aggregator import gh, gitsync


@pytest.fixture(autouse=True)
def no_real_gh(monkeypatch):
    """No test may create real releases or issues: the real `gh` is replaced by a failing stub.
    Tests pass their own stub, or call the saved original (test_gh.py) with subprocess stubbed."""

    def refuse(args, stdin=None):
        raise AssertionError(f"a test called the real gh: gh {' '.join(args)}")

    monkeypatch.setattr(gh, "run_gh", refuse)


@pytest.fixture(autouse=True)
def no_real_git(monkeypatch):
    """No test may commit or push in the project repo. Tests that need git run the saved
    original against their own repos in tmp_path (test_gitsync.py)."""

    def refuse(args, cwd):
        raise AssertionError(f"a test called the real git: git {' '.join(args)} in {cwd}")

    monkeypatch.setattr(gitsync, "run_git", refuse)
