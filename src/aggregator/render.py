"""Digest documents: YAML front matter plus a Markdown body.

A digest file looks like this (spec section 6):

    ---
    level: daily
    period: '2026-10-15'
    generated_at: '2026-10-15T06:03:12Z'
    model: claude-sonnet-5-5
    inputs:
    - heise
    - nyt-tech
    ---

    ## Tech
    - ...

Markdown to HTML email rendering will also live here (see ARCHITECTURE.md).
"""

from __future__ import annotations

from typing import Self

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from aggregator.schedule import Level, period_start

DELIMITER = "---"


class DigestFormatError(ValueError):
    """A digest document has no front matter or its front matter is invalid."""


class FrontMatter(BaseModel):
    # A hand-edited file may lose the quotes around a period like 2026, which YAML then reads as
    # an int; coerce it back to a string instead of rejecting it.
    model_config = ConfigDict(extra="forbid", frozen=True, coerce_numbers_to_str=True)

    level: Level
    period: str
    generated_at: AwareDatetime
    model: str = Field(min_length=1)
    # What the digest was built from: source ids for a daily digest, the periods of the
    # lower-level digests for the others.
    inputs: list[str] = []

    @field_validator("model")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def _period_matches_level(self) -> Self:
        period_start(self.level, self.period)  # raises ValueError for a malformed label
        return self


class Digest(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: FrontMatter
    body: str


def render_digest(digest: Digest) -> str:
    """Serialize a digest to the text of its Markdown file."""
    data = digest.meta.model_dump(mode="json")
    front = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"{DELIMITER}\n{front}{DELIMITER}\n\n{digest.body}"


def parse_digest(text: str) -> Digest:
    """Parse the text of a digest file. The inverse of `render_digest`."""
    text = text.replace("\r\n", "\n")
    if not text.startswith(DELIMITER + "\n"):
        raise DigestFormatError("missing front matter: the document must start with '---'")
    rest = text[len(DELIMITER) + 1 :]
    if rest.startswith(DELIMITER + "\n"):
        front, body = "", rest[len(DELIMITER) + 1 :]
    else:
        front, sep, body = rest.partition(f"\n{DELIMITER}\n")
        if not sep:
            raise DigestFormatError("unterminated front matter: no closing '---' line")
    body = body.removeprefix("\n")  # the blank line written by render_digest

    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise DigestFormatError(f"invalid YAML in front matter\n{exc}") from None
    if not isinstance(data, dict):
        raise DigestFormatError("front matter must be a mapping")
    try:
        meta = FrontMatter.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        raise DigestFormatError(f"invalid front matter: {problems}") from None
    return Digest(meta=meta, body=body)
