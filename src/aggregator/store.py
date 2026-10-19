"""Read and write digest files under `digests/` (spec section 6).

    digests/daily/YYYY/YYYY-MM-DD.md
    digests/weekly/YYYY/YYYY-Www.md     (YYYY is the ISO week-numbering year)
    digests/monthly/YYYY/YYYY-MM.md
    digests/yearly/YYYY.md

Committing and pushing the files is a separate step and will be added here later.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from aggregator.config import PROJECT_ROOT
from aggregator.render import Digest, DigestFormatError, parse_digest, render_digest
from aggregator.schedule import Level, period_start

DEFAULT_DIGESTS_DIR = PROJECT_ROOT / "digests"


def digest_path(level: Level, period: str, root: Path | str = DEFAULT_DIGESTS_DIR) -> Path:
    """Path of the digest for `period` (a label from `schedule.period_label`).

    Raises `ValueError` for an unknown level or a malformed period, so the result always lies
    inside `root`.
    """
    period_start(level, period)  # validates the label
    root = Path(root)
    if level == "yearly":
        return root / level / f"{period}.md"
    # The label starts with its year; for weeks this is the ISO year (2025-12-29 is in 2026-W01).
    return root / level / period[:4] / f"{period}.md"


def digest_exists(level: Level, period: str, root: Path | str = DEFAULT_DIGESTS_DIR) -> bool:
    """Idempotency guard: whether the digest for `period` has already been written."""
    return digest_path(level, period, root).is_file()


def write_digest(
    digest: Digest, root: Path | str = DEFAULT_DIGESTS_DIR, *, overwrite: bool = False
) -> Path:
    """Write `digest` to its path and return the path.

    Raises `FileExistsError` if the file exists, unless `overwrite` is set (the `--force` case).
    The file is written to a temporary file first and then moved into place, so an interrupted
    run never leaves a half-written digest that the exists guard would accept.
    """
    path = digest_path(digest.meta.level, digest.meta.period, root)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path}: digest already exists")
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_digest(digest))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_digest(level: Level, period: str, root: Path | str = DEFAULT_DIGESTS_DIR) -> Digest:
    """Read and parse a digest. Raises `FileNotFoundError` if it does not exist and
    `DigestFormatError` if its front matter is invalid or does not match its path."""
    path = digest_path(level, period, root)
    digest = read_digest_file(path)
    if (digest.meta.level, digest.meta.period) != (level, period):
        raise DigestFormatError(
            f"{path}: front matter says {digest.meta.level} {digest.meta.period}, "
            f"expected {level} {period}"
        )
    return digest


def read_digest_file(path: Path | str) -> Digest:
    """Read and parse the digest file at `path`, with the path in any error message."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        return parse_digest(text)
    except DigestFormatError as exc:
        raise DigestFormatError(f"{path}: {exc}") from None
