from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aggregator.render import Digest, DigestFormatError, FrontMatter
from aggregator.schedule import LEVELS, period_label
from aggregator.store import (
    DEFAULT_DIGESTS_DIR,
    digest_exists,
    digest_path,
    read_digest,
    read_digest_file,
    write_digest,
)

DAY = date(2026, 10, 15)


def make_digest(level="daily", period=None, body="Body text.\n") -> Digest:
    meta = FrontMatter(
        level=level,
        period=period or period_label(level, DAY),
        generated_at=datetime(2026, 10, 15, 6, tzinfo=UTC),
        model="claude-sonnet-5-5",
        inputs=["a", "b"],
    )
    return Digest(meta=meta, body=body)


@pytest.mark.parametrize(
    ("level", "period", "relative"),
    [
        ("daily", "2026-10-15", "daily/2026/2026-10-15.md"),
        ("weekly", "2026-W42", "weekly/2026/2026-W42.md"),
        ("weekly", "2026-W01", "weekly/2026/2026-W01.md"),  # contains 2025-12-29
        ("monthly", "2026-10", "monthly/2026/2026-10.md"),
        ("yearly", "2026", "yearly/2026.md"),
    ],
)
def test_digest_path(tmp_path, level, period, relative):
    assert digest_path(level, period, tmp_path) == tmp_path / relative


def test_default_root_is_the_project_digests_dir():
    assert DEFAULT_DIGESTS_DIR.name == "digests"
    assert (DEFAULT_DIGESTS_DIR.parent / "pyproject.toml").is_file()
    assert digest_path("yearly", "2026") == DEFAULT_DIGESTS_DIR / "yearly" / "2026.md"


@pytest.mark.parametrize(
    ("level", "period"),
    [
        ("daily", "../../etc/passwd"),
        ("daily", "2026-10"),
        ("weekly", "2026-W54"),
        ("monthly", "2026-13"),
        ("yearly", "26"),
        ("hourly", "2026"),
    ],
)
def test_digest_path_rejects_invalid_periods(tmp_path, level, period):
    with pytest.raises(ValueError):
        digest_path(level, period, tmp_path)


@pytest.mark.parametrize("level", LEVELS)
def test_round_trip_for_every_level(tmp_path, level):
    digest = make_digest(level)
    path = write_digest(digest, tmp_path)
    assert path == digest_path(level, digest.meta.period, tmp_path)
    assert read_digest(level, digest.meta.period, tmp_path) == digest
    assert read_digest_file(path) == digest


@pytest.mark.parametrize("level", LEVELS)
def test_exists_guard_for_every_level(tmp_path, level):
    period = period_label(level, DAY)
    assert not digest_exists(level, period, tmp_path)

    write_digest(make_digest(level), tmp_path)

    assert digest_exists(level, period, tmp_path)
    # Only that level and period, not its neighbours or other levels.
    assert not digest_exists(level, period_label(level, date(2024, 1, 1)), tmp_path)
    for other in LEVELS:
        if other != level:
            assert not digest_exists(other, period_label(other, DAY), tmp_path)


def test_exists_ignores_a_directory_at_the_digest_path(tmp_path):
    digest_path("yearly", "2026", tmp_path).mkdir(parents=True)
    assert not digest_exists("yearly", "2026", tmp_path)


def test_write_refuses_to_overwrite_unless_forced(tmp_path):
    write_digest(make_digest(body="first\n"), tmp_path)
    with pytest.raises(FileExistsError):
        write_digest(make_digest(body="second\n"), tmp_path)
    assert read_digest("daily", "2026-10-15", tmp_path).body == "first\n"

    write_digest(make_digest(body="second\n"), tmp_path, overwrite=True)
    assert read_digest("daily", "2026-10-15", tmp_path).body == "second\n"


def test_write_leaves_no_temporary_files(tmp_path):
    path = write_digest(make_digest(), tmp_path)
    assert list(path.parent.iterdir()) == [path]


def test_write_uses_lf_line_endings(tmp_path):
    path = write_digest(make_digest(body="a\nb\n"), tmp_path)
    assert b"\r\n" not in path.read_bytes()


def test_read_missing_digest(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_digest("daily", "2026-10-15", tmp_path)


def test_read_rejects_front_matter_that_does_not_match_the_path(tmp_path):
    digest = make_digest()
    source = write_digest(digest, tmp_path)
    other = digest_path("daily", "2026-10-16", tmp_path)
    Path(source).rename(other)
    with pytest.raises(DigestFormatError, match="expected daily 2026-10-16"):
        read_digest("daily", "2026-10-16", tmp_path)


def test_read_error_names_the_file(tmp_path):
    path = digest_path("daily", "2026-10-15", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("no front matter\n", encoding="utf-8")
    with pytest.raises(DigestFormatError, match=str(path)):
        read_digest("daily", "2026-10-15", tmp_path)
