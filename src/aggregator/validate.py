"""Checks for the Markdown that Claude returns (architecture 3.4).

`check_daily` lists every rule a daily digest body breaks, as sentences that can be sent back to
Claude in the retry request. `truncate_bullets` is the fallback for a digest that is still too
long after the retry.

The body format is (spec 5):

    ## Tech
    ### AI
    - bullet [1]
    - bullet [2, 5]
    ## SAP
    - bullet [3]

Each daily bullet ends with the indices of the input documents it is based on; code replaces them
with links to the articles (`render.link_citations`).

Every list item counts as a bullet, nested or not, so nesting cannot hide bullets from the limit.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+\S")
# The citation at the end of a bullet: "[3]" or "[3, 7]"; "[3][7]" and "[3], [7]" are accepted.
_CITATION = re.compile(r"\s*((?:,?\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\])+)\s*$")


class Kind(StrEnum):
    SECTION = "section"  # ## heading
    SUBSECTION = "subsection"  # ### heading
    OTHER_HEADING = "other-heading"  # #, ####, ...
    BULLET = "bullet"
    CONTINUATION = "continuation"  # an indented line that wraps the bullet above
    TEXT = "text"
    BLANK = "blank"


@dataclass(frozen=True)
class Line:
    kind: Kind
    raw: str
    title: str = ""  # heading text
    nested: bool = False  # an indented list item


def parse(body: str) -> list[Line]:
    lines: list[Line] = []
    in_bullet = False
    for raw in body.replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            lines.append(Line(Kind.BLANK, raw))
            continue
        if heading := _HEADING.match(raw):
            depth, title = len(heading.group(1)), heading.group(2)
            kind = {2: Kind.SECTION, 3: Kind.SUBSECTION}.get(depth, Kind.OTHER_HEADING)
            lines.append(Line(kind, raw, title=title))
            in_bullet = False
        elif item := _LIST_ITEM.match(raw):
            lines.append(Line(Kind.BULLET, raw, nested=bool(item.group(1))))
            in_bullet = True
        elif in_bullet and raw[:1].isspace():
            lines.append(Line(Kind.CONTINUATION, raw))
        else:
            lines.append(Line(Kind.TEXT, raw))
            in_bullet = False
    return lines


def count_bullets(body: str) -> int:
    return sum(line.kind is Kind.BULLET for line in parse(body))


def bullet_ends(lines: Sequence[Line]) -> list[int]:
    """The index of the last line of each bullet (the bullet line or its last wrapped line)."""
    ends: list[int] = []
    for index, line in enumerate(lines):
        if line.kind is Kind.BULLET:
            ends.append(index)
        elif line.kind is Kind.CONTINUATION and ends:
            ends[-1] = index
    return ends


def split_citation(text: str) -> tuple[str, list[int]]:
    """The text without its trailing citation, and the cited indices (empty if none)."""
    match = _CITATION.search(text)
    if not match:
        return text, []
    return text[: match.start()], [int(n) for n in re.findall(r"\d+", match.group(1))]


def citation_problems(body: str, documents: int) -> list[str]:
    """Bullets without a citation, and citations of documents that do not exist."""
    lines = parse(body)
    uncited: list[str] = []
    unknown: set[int] = set()
    for end in bullet_ends(lines):
        _, cited = split_citation(lines[end].raw)
        if not cited:
            uncited.append(lines[end].raw.strip())
        unknown.update(n for n in cited if not 1 <= n <= documents)
    problems = []
    if uncited:
        problems.append(
            f"{len(uncited)} bullet(s) do not end with the index numbers of the documents they "
            f'are based on, like "[3]" or "[3, 7]". First: "{_shorten(uncited[0])}".'
        )
    if unknown:
        problems.append(
            "Bullets cite documents that do not exist: "
            + ", ".join(str(n) for n in sorted(unknown))
            + f". Valid index numbers are 1 to {documents}."
        )
    return problems


def bullet_limit_problem(body: str, max_bullets: int) -> str | None:
    count = count_bullets(body)
    if count <= max_bullets:
        return None
    return (
        f"The digest has {count} bullets, but at most {max_bullets} are allowed in total. "
        f"Keep only the {max_bullets} most significant ones, or merge related ones."
    )


def structure_problems(body: str, categories: Sequence[str]) -> list[str]:
    """Rule violations other than the bullet limit."""
    problems: list[str] = []
    allowed = list(categories)
    allowed_text = ", ".join(allowed)
    seen: list[str] = []
    section: str | None = None
    section_bullets: dict[str, int] = {}
    stray: list[str] = []
    bad_headings: list[str] = []
    orphan_subsections: list[str] = []
    nested = 0

    for line in parse(body):
        if line.kind is Kind.SECTION:
            section = line.title
            if section not in allowed:
                problems.append(
                    f'"## {section}" is not an allowed section. Top-level sections must be '
                    f"exactly one of: {allowed_text}."
                )
            elif section in seen:
                problems.append(f'The section "## {section}" appears more than once.')
            seen.append(section)
            section_bullets.setdefault(section, 0)
        elif line.kind is Kind.SUBSECTION:
            if section is None:
                orphan_subsections.append(line.title)
        elif line.kind is Kind.OTHER_HEADING:
            bad_headings.append(line.raw.strip())
        elif line.kind is Kind.BULLET:
            nested += line.nested
            if section is None:
                stray.append(line.raw.strip())
            else:
                section_bullets[section] += 1
        elif line.kind is Kind.TEXT:
            stray.append(line.raw.strip())

    if not section_bullets:
        problems.append("The digest has no '## ' sections.")
    if bad_headings:
        problems.append(
            "Only '## ' section and '### ' sub-section headings are allowed, found: "
            + "; ".join(f'"{h}"' for h in bad_headings[:3])
            + "."
        )
    if orphan_subsections:
        problems.append("'### ' sub-sections must be inside a '## ' section.")
    if stray:
        problems.append(
            "All content must be '- ' bullets inside a section, with no title, introduction or "
            f'other text. Found: "{_shorten(stray[0])}".'
        )
    if nested:
        problems.append("Bullets must not be nested.")
    empty = [s for s, n in section_bullets.items() if n == 0]
    if empty:
        problems.append(
            "Leave out sections without bullets: " + ", ".join(f'"## {s}"' for s in empty) + "."
        )
    order = [s for s in allowed if s in section_bullets]
    if [s for s in dict.fromkeys(seen) if s in allowed] != order:
        problems.append(f"Sections must be in this order: {', '.join(order)}.")
    return problems


def check_daily(
    body: str, categories: Sequence[str], max_bullets: int, documents: int | None = None
) -> list[str]:
    """Every rule the daily body breaks; empty when it is valid. Citations are checked when the
    number of input `documents` is given."""
    problems = structure_problems(body, categories)
    if limit := bullet_limit_problem(body, max_bullets):
        problems.insert(0, limit)
    if documents is not None:
        problems += citation_problems(body, documents)
    return problems


def truncate_bullets(body: str, max_bullets: int) -> str:
    """Keep the first `max_bullets` bullets (with their wrapped lines) in their order and drop
    the rest, then drop headings left without bullets."""
    kept: list[Line] = []
    bullets = 0
    keeping = True
    for line in parse(body):
        if line.kind is Kind.BULLET:
            bullets += 1
            keeping = bullets <= max_bullets
        if line.kind is Kind.CONTINUATION or line.kind is Kind.BULLET:
            if keeping:
                kept.append(line)
            continue
        kept.append(line)
    return _render(_drop_empty_headings(kept))


def _drop_empty_headings(lines: list[Line]) -> list[Line]:
    """Remove each ## or ### heading that has no bullet before the next heading of the same or a
    higher level."""
    result: list[Line] = []
    for index, line in enumerate(lines):
        if line.kind in (Kind.SECTION, Kind.SUBSECTION) and not _has_bullets(lines, index):
            continue
        result.append(line)
    return result


def _has_bullets(lines: list[Line], index: int) -> bool:
    stops = {Kind.SECTION} if lines[index].kind is Kind.SECTION else {Kind.SECTION, Kind.SUBSECTION}
    for line in lines[index + 1 :]:
        if line.kind in stops:
            return False
        if line.kind is Kind.BULLET:
            return True
    return False


def _render(lines: list[Line]) -> str:
    text = "\n".join(line.raw for line in lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n" if text else ""


def _shorten(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
