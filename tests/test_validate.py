from __future__ import annotations

import pytest

from aggregator.validate import (
    bullet_ends,
    check_daily,
    citation_problems,
    count_bullets,
    parse,
    split_citation,
    structure_problems,
    truncate_bullets,
)

CATEGORIES = ["Tech", "SAP", "Business", "Other"]

VALID = """\
## Tech
### AI
- A lab released a new open-weights model.
- Another lab cut API prices.
### Cloud & Dev
- A major cloud region had an outage.
## SAP
- SAP announced a new S/4HANA release.
## Other
- Something else happened.
"""


def bullets(n: int, section: str = "Tech") -> str:
    return f"## {section}\n" + "".join(f"- Bullet {i}.\n" for i in range(1, n + 1))


# --- counting -----------------------------------------------------------------------------------


def test_counts_every_list_item_including_nested_and_numbered():
    body = "## Tech\n- one\n  - nested\n* star\n+ plus\n1. numbered\n2) also\n"
    assert count_bullets(body) == 6


def test_wrapped_bullet_lines_count_once():
    assert count_bullets("## Tech\n- a long bullet\n  that wraps\n- second\n") == 2


def test_headings_and_horizontal_text_are_not_bullets():
    assert count_bullets("## Tech\n-not a bullet\n---\n") == 0


# --- check_daily --------------------------------------------------------------------------------


def test_valid_digest_has_no_problems():
    assert check_daily(VALID, CATEGORIES, 10) == []


def test_exactly_at_the_limit_is_valid():
    assert check_daily(bullets(10), CATEGORIES, 10) == []


def test_over_the_limit_names_the_count_and_the_limit():
    problems = check_daily(bullets(12), CATEGORIES, 10)
    assert len(problems) == 1
    assert "12 bullets" in problems[0] and "at most 10" in problems[0]


def test_the_limit_is_the_first_problem():
    problems = check_daily("Intro\n" + bullets(11), CATEGORIES, 10)
    assert "11 bullets" in problems[0] and len(problems) == 2


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## World\n- x\n", "not an allowed section"),
        ("## tech\n- x\n", "not an allowed section"),
        ("## Tech\n- x\n## Tech\n- y\n", "more than once"),
        ("- x\n- y\n", "no '## ' sections"),
        ("# Daily digest\n## Tech\n- x\n", "Only '## ' section"),
        ("## Tech\n#### Deep\n- x\n", "Only '## ' section"),
        ("### AI\n- x\n## Tech\n- y\n", "inside a '## ' section"),
        ("Here is your digest:\n\n## Tech\n- x\n", 'Found: "Here is your digest:"'),
        ("## Tech\n- x\n\nSources: a, b\n", "no title, introduction"),
        ("## Tech\n- x\n  - y\n", "must not be nested"),
        ("## Tech\n## SAP\n- x\n", 'Leave out sections without bullets: "## Tech"'),
        ("## SAP\n- x\n## Tech\n- y\n", "in this order: Tech, SAP"),
    ],
)
def test_structure_problems(body, expected):
    problems = structure_problems(body, CATEGORIES)
    assert any(expected in p for p in problems), problems


def test_unknown_section_message_lists_the_allowed_categories():
    (problem,) = structure_problems("## World\n- x\n", CATEGORIES)
    assert "Tech, SAP, Business, Other" in problem


def test_trailing_hashes_in_headings_are_ignored():
    assert structure_problems("## Tech ##\n- x\n", CATEGORIES) == []


def test_sub_sections_are_free_form():
    assert structure_problems("## Tech\n### Anything Goes\n- x\n", CATEGORIES) == []


# --- truncate_bullets ---------------------------------------------------------------------------


def test_truncation_keeps_the_first_bullets_in_order():
    body = "## Tech\n- a\n- b\n## SAP\n- c\n- d\n"
    assert truncate_bullets(body, 3) == "## Tech\n- a\n- b\n## SAP\n- c\n"


def test_truncation_drops_sections_and_sub_sections_left_empty():
    body = "## Tech\n### AI\n- a\n### Cloud\n- b\n## SAP\n- c\n## Other\n- d\n"
    assert truncate_bullets(body, 1) == "## Tech\n### AI\n- a\n"


def test_truncation_keeps_wrapped_lines_of_kept_bullets_only():
    body = "## Tech\n- a\n  wraps\n- b\n  also wraps\n"
    assert truncate_bullets(body, 1) == "## Tech\n- a\n  wraps\n"


def test_truncation_keeps_blank_line_layout_tidy():
    body = "## Tech\n\n- a\n\n## SAP\n\n- b\n\n## Other\n\n- c\n"
    result = truncate_bullets(body, 2)
    assert result == "## Tech\n\n- a\n\n## SAP\n\n- b\n"
    assert count_bullets(result) == 2


def test_truncation_of_a_valid_digest_changes_nothing():
    assert truncate_bullets(VALID, 10) == VALID


# --- citations ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rest", "cited"),
    [
        ("- A story. [3]", "- A story.", [3]),
        ("- A story. [3, 7]", "- A story.", [3, 7]),
        ("- A story.[3,7]  ", "- A story.", [3, 7]),
        ("- A story. [3][7]", "- A story.", [3, 7]),
        ("- A story. [3], [7]", "- A story.", [3, 7]),
        ("- A story.", "- A story.", []),
        ("- [3] at the start does not count.", "- [3] at the start does not count.", []),
        ("- A story. [see 3]", "- A story. [see 3]", []),
    ],
)
def test_split_citation(text, rest, cited):
    assert split_citation(text) == (rest, cited)


def test_valid_citations_have_no_problems():
    body = "## Tech\n- One. [1]\n- Two,\n  wrapped. [2, 3]\n"
    assert citation_problems(body, 3) == []


def test_uncited_bullets_are_reported():
    (problem,) = citation_problems("## Tech\n- One.\n- Two. [1]\n- Three.\n", 3)
    assert problem.startswith("2 bullet(s) do not end with the index numbers")
    assert '"- One."' in problem


def test_unknown_documents_are_reported():
    (problem,) = citation_problems("## Tech\n- One. [0, 1]\n- Two. [9]\n", 3)
    assert problem == (
        "Bullets cite documents that do not exist: 0, 9. Valid index numbers are 1 to 3."
    )


def test_check_daily_checks_citations_only_with_a_document_count():
    body = "## Tech\n- One.\n"
    assert check_daily(body, CATEGORIES, 10) == []
    assert "do not end with the index numbers" in check_daily(body, CATEGORIES, 10, 2)[0]


def test_bullet_ends_include_wrapped_lines():
    lines = parse("## Tech\n- One\n  wrapped\n- Two\n")
    assert [lines[i].raw for i in bullet_ends(lines)] == ["  wrapped", "- Two"]
