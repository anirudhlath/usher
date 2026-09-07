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

**M10's D5 adds a second subject to this file, and it is one panel rather than
every section.** Dashboard 5's cache-age panel is the only panel in the six
dashboards whose failure is a *licence breach* rather than a blind spot — TMDb's
≤6-month caching term — so what it names is worth pinning by name and not merely
counting as "backed". Two spellings have to stay dead: `titles.enriched_at`,
which is when *Usher* enriched rather than when the *payload* was cached, and
`provider_cache_meta`, a table
[ADR-0016](../../docs/prd/decisions/0016-raw-payloads-cache-providers-not-sources.md)
refused by name and which no migration has ever created. Both were live in this
document or in M10's spec, and a compliance ceiling stated against a column that
answers a different question — or a table that does not exist — is worse than no
panel at all, because it reads as enforcement.

`compliance_panel_sql` is here rather than in the integration suite so that the
PRD stays the single source for the panel's own SQL:
`tests/integration/test_raw_payload_cache_age.py` executes exactly what this
module extracts, and D10's dashboard JSON copies the same block. A second
transcription is a second thing to keep in step.
"""

import pathlib
import re

import pytest

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


# The panel's own SQL, as PRD 10 spells it, inside dashboard 5's section. Scoped
# to that section rather than to the document: `## Analytics tables` carries a
# second ```sql fence, and `_dashboards` already excludes it by construction.
_SQL_FENCE = re.compile(r"^```sql\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)


def normalised(text: str) -> str:
    """Prose with its line wrapping collapsed.

    Every substring check against this document has to go through here.
    `_BACKING_CLAIM` already normalises for the same reason — "the wrong column"
    is one reflow away from being "the wrong\\ncolumn", and a check that reads
    the raw text goes green again the moment someone rewraps the paragraph,
    which is the *silent* direction.
    """
    return " ".join(text.split())


def section_body(number: int, text: str | None = None) -> str:
    """One dashboard section's body, by its heading number."""
    sections = _sections(text if text is not None else _PRD.read_text(encoding="utf-8"))
    bodies = [body for found, _, body in sections if found == number]
    assert len(bodies) == 1, (
        f"expected exactly one '### {number} — …' section under '## Dashboards', found "
        f"{len(bodies)} — zero means the heading moved and every check reading this "
        "section now reads nothing"
    )
    return bodies[0]


def compliance_panel_sql(text: str | None = None) -> list[str]:
    """Dashboard 5's cache-age panel, as three statements PRD 10 spells out.

    **Three targets and not one statement**, which is a measurement rather than
    a preference: folding the oldest entry and the two counts into a single
    aggregate costs `ix_raw_payloads_fetched_at` its work, because `count(*)`
    over the whole table has to read every row and the planner then seq-scans
    the lot. Measured on the live catalog 2026-09-07 at 133,501 payloads —
    separately the three plan at 0.48, 4.46 and 4,813; combined, one
    `Parallel Seq Scan` and the index unused. `test_raw_payload_cache_age.py`
    is what keeps that true.

    The split is on `;`, which is safe only because no statement in the block
    contains one; the count guard below is what notices if that changes.
    """
    fence = _SQL_FENCE.search(section_body(5, text))
    assert fence is not None, (
        "dashboard 5 carries no ```sql fence, so the compliance panel has no SQL in the "
        "PRD and everything reading it here checks nothing"
    )
    return [statement.strip() for statement in fence["body"].split(";") if statement.strip()]


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


def test_dashboard_5_names_the_compliance_series_and_the_column_it_is_not() -> None:
    """The acceptance is *both* halves. Naming `raw_payloads.fetched_at` fixes
    the panel; naming `titles.enriched_at` beside it as the wrong answer is what
    stops the next reader re-conflating them, which is how the wrong column got
    here in the first place. A correction that deletes the mistake teaches
    nobody why it was one."""
    body = normalised(section_body(5))

    assert "`raw_payloads.fetched_at`" in body, (
        "dashboard 5 does not name the column ADR-0016 made the compliance answer"
    )
    assert "`titles.enriched_at`" in body, (
        "the distinction from titles.enriched_at is not stated, so nothing here stops it "
        "being re-conflated with the payload's own timestamp"
    )
    assert "wrong column" in body, (
        "titles.enriched_at is named without being marked wrong, which reads as a second "
        "valid series rather than as the refused one"
    )


def test_the_panel_sql_is_three_statements_over_raw_payloads() -> None:
    """The premise every other check on this block depends on, and the one the
    integration suite executes. Three, because folding them into one statement
    is what costs the index — so a block that has been "simplified" back to a
    single aggregate fails here rather than silently in the plan."""
    statements = compliance_panel_sql()

    assert len(statements) == 3, (
        f"the compliance panel is three targets, not {len(statements)} — folding the "
        "oldest entry and the counts into one aggregate loses ix_raw_payloads_fetched_at, "
        "which is measured in test_raw_payload_cache_age.py"
    )
    for statement in statements:
        assert "raw_payloads" in statement, f"not a query against the cache:\n{statement}"
        assert "select" in statement.lower(), f"not a readable target:\n{statement}"


def test_the_panel_sql_carries_all_three_numbers_and_the_threshold() -> None:
    """`min(fetched_at)` alone is satisfied by a cache holding one ancient row
    and says nothing about how much of the cache is out of term, which is the
    question a licence breach is measured in. The numbers are asserted on the
    SQL rather than on the prose because the SQL is what D10 copies."""
    joined = "\n".join(compliance_panel_sql())

    assert "min(fetched_at)" in joined, "the oldest entry is missing"
    assert "past_ceiling" in joined, "the count of entries past the ceiling is missing"
    assert "past_ceiling_share" in joined, "the count as a share of count(*) is missing"
    assert "count(*)" in joined, "the denominator the share is taken over is missing"
    assert "now() - interval '6 months'" in joined, (
        "the ceiling is not spelled as a 6-month interval, so the threshold line and the "
        "count are no longer read against the term TMDb actually states"
    )
    assert "threshold line" in normalised(section_body(5)), (
        "the panel does not say it carries a threshold line, so a dashboard could ship "
        "three numbers with nothing to read them against"
    )


def test_the_panel_sql_never_names_the_column_or_the_table_adr_0016_refused() -> None:
    """`titles.enriched_at` and `provider_cache_meta` are both live spellings —
    the first shipped in this document until 2026-08-14, the second is still in
    M10's spec. The prose above has to *mention* both to correct them, so this
    arm is on the SQL, where a mention is a defect rather than an explanation."""
    joined = "\n".join(compliance_panel_sql())

    assert "enriched_at" not in joined, (
        f"the panel's own SQL reads an enrichment timestamp, not the payload's:\n{joined}"
    )
    assert "provider_cache_meta" not in joined, (
        f"the panel's own SQL reads a table no migration creates:\n{joined}"
    )
    assert "fetched_at" in joined, "the positive control: the SQL reads no timestamp at all"


def test_the_single_number_version_is_recorded_as_a_rejected_design() -> None:
    """Stated in the panel's own description rather than in a plan, because a
    plan is not read by whoever trims a dashboard for time. Three numbers cost
    more than one and the reason has to travel with them."""
    body = normalised(section_body(5))

    assert "rejected design" in body, (
        "nothing marks the single-number version as rejected, so the panel can be "
        "'simplified' back to min(fetched_at) by someone who reads it as an accident"
    )


def test_prd_10_mentions_provider_cache_meta_only_where_it_denies_it_exists() -> None:
    """The whole document, not just dashboard 5. A table refused by name in
    ADR-0016 may appear here only as a refusal — anywhere else it reads as
    schema, which is what M10's spec still does."""
    text = _PRD.read_text(encoding="utf-8")
    denials = ("does not exist", "refused it by name", "is not created")

    windows = []
    start = 0
    while (found := text.find("provider_cache_meta", start)) != -1:
        windows.append(normalised(text[max(0, found - 400) : found + 400]))
        start = found + 1

    assert windows, (
        "no mention of provider_cache_meta at all — this scan globs nothing and so cannot "
        "fail; the panel's correction has been deleted along with the name it corrects"
    )
    unqualified = [window for window in windows if not any(d in window for d in denials)]
    assert unqualified == [], (
        f"provider_cache_meta is named without being denied, which reads as a table that "
        f"exists: {unqualified}"
    )


def test_the_section_accessor_refuses_a_number_that_is_not_there() -> None:
    """Proved on a synthetic document: a `section_body` that returned `""` for a
    heading that moved would make every check above pass on an empty string."""
    document = (
        "## Dashboards\n\nOne.\n\n"
        "### 1 — Only\n\nPanel a.\n\n"
        "## Where the stack lives\n\nElsewhere.\n"
    )

    assert "Panel a." in section_body(1, document)
    with pytest.raises(AssertionError, match="expected exactly one"):
        section_body(5, document)


def test_the_sql_extractor_refuses_a_section_carrying_no_fence() -> None:
    """The other half of the same hazard: an extractor returning `[]` would let
    every `in joined` assertion above pass vacuously on the empty string, and
    `len(statements) == 3` is the only one that would notice."""
    bare = "## Dashboards\n\nOne.\n\n### 5 — Cost & Compliance\n\nPanel a.\n\n## Elsewhere\n\nx.\n"
    fenced = (
        "## Dashboards\n\nOne.\n\n### 5 — Cost & Compliance\n\nPanel a.\n\n"
        "```sql\nSELECT 1 FROM raw_payloads;\n```\n\n## Elsewhere\n\nx.\n"
    )

    with pytest.raises(AssertionError, match="no ```sql fence"):
        compliance_panel_sql(bare)
    assert compliance_panel_sql(fenced) == ["SELECT 1 FROM raw_payloads"]
