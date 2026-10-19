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

The Sources section at the bottom of a daily digest is rendered here by code, never by the LLM
(architecture 3.4), from the sources that actually contributed items to the digest:

    ## Sources
    - Hacker News
    - SAP News Center

    ### Warnings
    - The Verge: failed 3 runs in a row. Fix or remove the source.

The Warnings sub-section is the slot for source health warnings (#25) and is left out when there
are none.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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

from aggregator.config import Source
from aggregator.schedule import Level, period_start
from aggregator.sources import FetchResult

DELIMITER = "---"
SOURCES_HEADING = "## Sources"
WARNINGS_HEADING = "### Warnings"


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


# --- daily Sources section --------------------------------------------------------------------


@dataclass(frozen=True)
class SourceWarning:
    """A health warning for one source, e.g. failing or stale (#25)."""

    source_name: str
    message: str


def contributing_sources(sources: Iterable[Source], results: Iterable[FetchResult]) -> list[Source]:
    """The sources that contributed at least one item to the digest, in config order.

    `results` are the fetch results after dedupe (`dedupe.select_new`), so a source whose items
    were all seen before ("no new items") or that failed has no items and is not listed. Results
    for ids that are not in `sources` are ignored.
    """
    with_items = {result.source_id for result in results if result.items}
    return [source for source in sources if source.id in with_items]


def _one_line(text: str) -> str:
    # Names come from sources.yaml; a quoted YAML string could contain a line break.
    return " ".join(text.split())


def render_sources_section(
    sources: Sequence[Source], warnings: Sequence[SourceWarning] = ()
) -> str:
    """The Markdown Sources section: source names only, then the health warnings if any.
    Empty when there is nothing to list."""
    if not sources and not warnings:
        return ""
    lines = [SOURCES_HEADING]
    lines += [f"- {_one_line(source.name)}" for source in sources]
    if warnings:
        if sources:
            lines.append("")
        lines.append(WARNINGS_HEADING)
        lines += [f"- {_one_line(w.source_name)}: {_one_line(w.message)}" for w in warnings]
    return "\n".join(lines) + "\n"


def with_sources_section(body: str, section: str) -> str:
    """The daily body with the Sources section appended at the bottom."""
    body = body.rstrip()
    if not section:
        return body + "\n" if body else ""
    return f"{body}\n\n{section}" if body else section
