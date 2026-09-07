"""A dashboard panel with no series behind it is indistinguishable from one
nobody has written yet — which is
[PRD 10](../../docs/prd/10-telemetry-and-dashboards.md)'s own first principle
("right datasource per question") applied to panels instead of to metrics.

Dashboards 3, 4, 5 and 6 each carry a paragraph that says, in bold, which of
their panels have a writer behind them and as of when. Dashboards 1 and 2 did
not, so nothing distinguished *"every panel here is backed"* from *"nobody has
checked"*. This module is the check that keeps them all annotated.

**The scan's own premises are asserted, because a scan that globs nothing
passes exactly like a scan that passes.** These headings are Markdown, and the
next reformat can move them: a stray blank line, an em-dash turned into a
hyphen, or a promotion of `### 1 — …` to `## 1 — …` would leave
`_DASHBOARD_HEADING` matching zero sections and `unbacked == []` trivially
true. So the count, the contiguity of the numbering and two headings by name
are asserted before the thing the module exists to check.

**Six sections, not five.** The task that commissioned this file was drafted
2026-08-13 and specified five; `### 6 — Quality evals` has landed since, with
its own `✅ **Backed by real data as of E1**` paragraph, and the document's own
preamble under `## Dashboards` reads "Six." The exact count is deliberate
rather than a `>=`: a seventh dashboard must come here and say so, which is the
same currency discipline `test_docs_currency.py` applies to the status tables.

**A backing statement is a *bolded* opening, and unbolded prose does not
satisfy it.** Section 5's first paragraph — "Data freshness is backed by real
data as of M2" — is exactly the shape that must not count on its own, and the
reason is the trap `test_docs_currency.py` records: a documentation check that
reads prose can be answered by prose written to explain the fix. The bold run
is the annotation; a sentence merely containing the words is narration.
Section 5 passes on its *second* paragraph, which is bolded.
`test_the_scan_refuses_an_unbolded_mention_of_backing` pins that arm.
"""

import pathlib
import re

_ROOT = pathlib.Path(__file__).parents[2]
_PRD = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"

_DASHBOARD_HEADING = re.compile(r"^### (?P<number>\d+) — (?P<title>.+)$", re.MULTILINE)

# The status markers PRD 10 opens annotated paragraphs with. ⚠️ and ⏳ are here
# because the vocabulary permits them, not because a dashboard uses them today.
_STATUS_MARKER = "[✅⚠️\U0001f534⏳]"

# A backing statement opens its paragraph: optional status marker, then a bold
# run. `DOTALL` because the run wraps — section 5's spans three lines.
_BOLD_OPENING = re.compile(rf"\A(?:{_STATUS_MARKER}\s*)*\*\*(?P<claim>.+?)\*\*", re.DOTALL)

# What the bold run has to say. Both spellings are in use: "backed by real data
# as of M4" (sections 3, 4, 5, 6) and, for a panel that has no writer, some form
# of "unbacked". Case-insensitive because section 6's run *opens* on the word —
# `**Backed by real data as of E1**` — and the run is whitespace-normalised
# before it is searched because sections 3 and 5 wrap theirs across a line
# break, so "backed by real\ndata as of M4" is the literal text on the page.
_BACKING_CLAIM = re.compile(r"backed by real data as of |unbacked", re.IGNORECASE)


def _dashboards(text: str) -> str:
    """The body of `## Dashboards`, up to the next level-2 heading."""
    start = re.search(r"^## Dashboards$", text, re.MULTILINE)
    assert start is not None, "10-telemetry-and-dashboards.md has no '## Dashboards' heading"
    rest = text[start.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _sections(text: str) -> list[tuple[int, str, str]]:
    """`(number, title, body)` for every `### N — Title` under `## Dashboards`."""
    body = _dashboards(text)
    found = list(_DASHBOARD_HEADING.finditer(body))
    out: list[tuple[int, str, str]] = []
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(body)
        out.append((int(match["number"]), match["title"].strip(), body[match.end() : end]))
    return out


def _carries_a_backing_statement(section_body: str) -> bool:
    """Does any paragraph in this section open with a bolded backing claim?"""
    for paragraph in re.split(r"\n\s*\n", section_body):
        opening = _BOLD_OPENING.match(paragraph.strip())
        if opening is None:
            continue
        if _BACKING_CLAIM.search(" ".join(opening["claim"].split())):
            return True
    return False


def test_every_dashboard_section_carries_a_backing_statement() -> None:
    sections = _sections(_PRD.read_text(encoding="utf-8"))

    assert len(sections) == 6, (
        f"expected six dashboard sections under '## Dashboards', scanned {len(sections)} "
        "— zero means the headings moved and this file now checks nothing; seven means a "
        "dashboard landed without registering here"
    )
    assert [number for number, _, _ in sections] == [1, 2, 3, 4, 5, 6], (
        f"the dashboards are not numbered 1..6: {[n for n, _, _ in sections]}"
    )
    headings = {title for _, title, _ in sections}
    assert "Cost & Compliance" in headings, f"the named anchor is missing from {headings}"
    assert {"Library & Catalog", "Taste & Watching"} <= headings, (
        f"the two dashboards this check was written for are missing from {headings}"
    )

    unbacked = [
        f"{number} — {title}"
        for number, title, body in sections
        if not _carries_a_backing_statement(body)
    ]
    assert unbacked == [], (
        "every dashboard section must state which of its panels have a series behind "
        f"them, in a paragraph opening with a bolded claim; missing from: {unbacked}"
    )


def test_the_scan_names_a_section_whose_backing_statement_is_missing() -> None:
    """The negative arm, proved on a synthetic document rather than by editing
    the real one — so this file's teeth do not depend on a plant being
    restored."""
    document = (
        "## Dashboards\n\nSix.\n\n"
        "### 1 — Annotated\n\nPanel a · panel b.\n\n"
        "**Both panels are backed by real data as of M4.**\n\n"
        "### 2 — Bare\n\nPanel c · panel d.\n\n"
        "## Where the stack lives\n\nElsewhere.\n"
    )

    sections = _sections(document)

    assert [(number, title) for number, title, _ in sections] == [(1, "Annotated"), (2, "Bare")]
    assert _carries_a_backing_statement(sections[0][2])
    assert not _carries_a_backing_statement(sections[1][2])


def test_a_section_body_stops_at_the_next_heading() -> None:
    """Truncating each body at the following heading is what makes the check
    per-*section*, and the case above cannot see it: its bare section is the
    last one, so a `_sections` that ran every body to the end of the document
    would leave that section's body unchanged and the case green. Measured
    2026-09-07 — replacing the `end` expression with `len(body)` survives the
    other three cases *and* the whole 4,000-case unit suite, because sections
    1..5 of the real file would each then contain section 6's `✅ **Backed by
    real data as of E1**` and the scan would report `[]` with five annotations
    deleted. The bare section goes **first** here, which is the only
    arrangement that can fail."""
    document = (
        "## Dashboards\n\nTwo.\n\n"
        "### 1 — Bare\n\nPanel a · panel b.\n\n"
        "### 2 — Annotated\n\nPanel c · panel d.\n\n"
        "**Both panels are backed by real data as of M4.**\n\n"
        "## Where the stack lives\n\nElsewhere.\n"
    )

    sections = _sections(document)

    assert [(number, title) for number, title, _ in sections] == [(1, "Bare"), (2, "Annotated")]
    assert not _carries_a_backing_statement(sections[0][2]), (
        "the bare section inherited the annotated section below it, so every section "
        "but the last is satisfied by any one annotation in the document"
    )
    assert _carries_a_backing_statement(sections[1][2])


def test_the_scan_refuses_an_unbolded_mention_of_backing() -> None:
    """Section 5's first paragraph is this shape and must not satisfy the check
    on its own; its second, bolded one is what does. A check a paragraph of
    prose can answer is answered by the prose that explains why it is red."""
    narrated = "\nPanel c · panel d.\n\nData freshness is backed by real data as of M2.\n"
    annotated = "\nPanel c · panel d.\n\n✅ **Data freshness is backed by real data as of M2.**\n"

    assert not _carries_a_backing_statement(narrated)
    assert _carries_a_backing_statement(annotated)


def test_a_bold_opening_that_makes_no_backing_claim_is_not_one() -> None:
    """`**quality ladder**` opens no paragraph today, but a panel list that
    began with a bolded panel name would satisfy a check that only looked for
    bold — so the claim's words are asserted, not its formatting."""
    bold_but_silent = "\n**Quality ladder** (4K/HDR/codec share broken down by decade).\n"

    assert _BOLD_OPENING.match(bold_but_silent.strip()) is not None
    assert not _carries_a_backing_statement(bold_but_silent)
