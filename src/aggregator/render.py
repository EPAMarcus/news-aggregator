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

The Sources section at the bottom of a daily digest is rendered here by code, never by the LLM
(architecture 3.4), from the sources that actually contributed items to the digest:

    ## Sources
    - Hacker News
    - SAP News Center

    ### Warnings
    - The Verge: failed 3 runs in a row. Fix or remove the source.

The Warnings sub-section is the slot for source health warnings (#25) and is left out when there
are none.

Each daily bullet ends with the sources it is based on, linked to the articles. Claude cites the
input documents by index (`[1, 3]`); `link_citations` replaces that with the source names and the
item URLs, so Claude never writes a URL:

    - A lab released a new model. ([Hacker News](<https://...>), [The Verge](<https://...>))
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Self
from urllib.parse import urlparse

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
from aggregator.sources import FetchResult, Item
from aggregator.validate import Kind, bullet_ends, parse, split_citation

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


# --- links behind the daily bullets -----------------------------------------------------------


def _link_target(url: str | None) -> str | None:
    """The URL as a Markdown link destination, or None if it is not a plain http(s) URL. Feed
    URLs are untrusted: the angle-bracket form cannot be broken out of without `<`, `>` or
    whitespace, which are rejected."""
    if not url or re.search(r"[\s<>]", url):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"<{url}>"


def _link_text(name: str) -> str:
    return re.sub(r"([\\\[\]])", r"\\\1", _one_line(name))


def render_citation(items: Sequence[Item], names: Mapping[str, str]) -> str:
    """'([Hacker News](<url>), [The Verge](<url>))'. Two items of one source are numbered."""
    labels = [names.get(item.source_id, item.source_id) for item in items]
    totals = {label: labels.count(label) for label in labels}
    seen: dict[str, int] = {}
    parts = []
    for item, label in zip(items, labels, strict=True):
        if totals[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            label = f"{label} {seen[label]}"
        target = _link_target(item.url)
        text = _link_text(label)
        parts.append(f"[{text}]({target})" if target else text)
    return f"({', '.join(parts)})"


def link_citations(body: str, items: Sequence[Item], names: Mapping[str, str]) -> str:
    """Replace the citation at the end of each bullet with links to the cited items.

    `items` are the input documents in the order Claude saw them (index 1 is `items[0]`), and
    `names` maps source ids to the names shown in the digest. Unknown indices are dropped, a
    citation of the same item twice is linked once, and a bullet without a valid citation keeps
    its text without one. Validation reports such bullets before (`validate.citation_problems`).
    """
    lines = parse(body)
    raws = [line.raw for line in lines]
    for end in bullet_ends(lines):
        text, cited = split_citation(raws[end])
        if not cited:
            continue
        indices = [n for n in dict.fromkeys(cited) if 1 <= n <= len(items)]
        # The same article from two feeds, or cited twice, is one link (the first citation).
        chosen: dict[str, Item] = {}
        for n in indices:
            chosen.setdefault(items[n - 1].url or items[n - 1].id, items[n - 1])
        raws[end] = (
            f"{text.rstrip()} {render_citation(list(chosen.values()), names)}"
            if chosen
            else text.rstrip()
        )
    return "\n".join(raws)


_CITED_LINK = r"\[(?:\\.|[^\]\\])*\]\(<[^<>\s]*>\)"
_CITED_NAME = r"[^()\[\],]+"
_CITED = rf"(?:{_CITED_LINK}|{_CITED_NAME})"
_RENDERED_CITATION = re.compile(rf"\s*\({_CITED}(?:,\s*{_CITED})*\)\s*$")


def without_sources(body: str) -> str:
    """A daily body as rollup input: without the Sources section and without the article links
    behind the bullets, which would only cost tokens. A trailing parenthesis without a link is
    kept, as it may be part of the text."""
    lines = parse(body)
    raws = [line.raw for line in lines]
    for end in bullet_ends(lines):
        match = _RENDERED_CITATION.search(raws[end])
        if match and "](<" in match.group(0):
            raws[end] = raws[end][: match.start()]
    for index, line in enumerate(lines):
        if line.kind is Kind.SECTION and line.raw.strip() == SOURCES_HEADING:
            raws = raws[:index]
            break
    text = "\n".join(raws).strip()
    return text + "\n" if text else ""
