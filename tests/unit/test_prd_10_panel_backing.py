"""A dashboard panel with no series behind it is indistinguishable from one nobody has
written yet — which is [PRD 10](../../docs/prd/10-telemetry-and-dashboards.md)'s own
first principle ("right datasource per question") applied to panels instead of to
"""

import json
import pathlib
import re
from typing import Any

import pytest

from tests.unit.grafana import panels

_ROOT = pathlib.Path(__file__).parents[2]
_PRD = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"
# The *second* transcription of the compliance block. The PRD is the source and
# this file is the deployed copy, which is why they are graded against each
# other rather than either being graded alone.
_DASHBOARD_FIVE = _ROOT / "dashboards" / "05-cost-and-compliance.json"

_DASHBOARD_HEADING = re.compile(r"^### (?P<number>\d+) — (?P<title>.+)$", re.MULTILINE)

# The status markers PRD 10 opens annotated paragraphs with. ⚠️ and ⏳ are here
# because the vocabulary permits them, not because a dashboard uses them today.
_STATUS_MARKER = "[✅⚠️\U0001f534⏳]"

# A backing statement opens its paragraph: optional status marker, then a bold
# run. `DOTALL` because the run wraps — section 5's spans three lines.
_BOLD_OPENING = re.compile(rf"\A(?:{_STATUS_MARKER}\s*)*\*\*(?P<claim>.+?)\*\*", re.DOTALL)

# What the bold run has to say.
_BACKING_CLAIM = re.compile(r"backed by real data as of |unbacked", re.IGNORECASE)


def _dashboards(text: str) -> str:
    """The body of `## Dashboards`, up to the next level-2 heading."""
    start = re.search(r"^## Dashboards$", text, re.MULTILINE)
    assert start is not None, "10-telemetry-and-dashboards.md has no '## Dashboards' heading"
    rest = text[start.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _dashboards_preamble(text: str | None = None) -> str:
    """`## Dashboards`'s own paragraphs, above the first `### N — …` heading.

    Scoped rather than taken over the whole section body because the claim
    being checked is about *the set* — how many there are, where they live,
    how they are provisioned — and every per-dashboard section below repeats
    words like "dashboard" and "JSON" for its own reasons.
    """
    body = _dashboards(text if text is not None else _PRD.read_text(encoding="utf-8"))
    first = _DASHBOARD_HEADING.search(body)
    return body[: first.start()] if first else body


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


def test_the_dashboards_preamble_names_a_path_that_exists_and_how_it_is_provisioned() -> None:
    """M10's D6 lands the first dashboard, and this is the arm that keeps the
    preamble's promise checkable rather than merely written.

    **The path is resolved on disk, which is the whole point.** The sentence
    this file's neighbour corrected in 2026-08-19 was wrong in exactly one way —
    it described JSON that did not exist — and a check that only asserted the
    *words* `dashboards/` would have passed on it the moment somebody typed
    them. Every path the preamble names in backticks and ending in `.json`,
    `.yml` or `/` has to be a real file or directory.

    The count is asserted too, because "six" and "one of them built" are two
    claims and the second is the one that ages: D7 through D10 each make it
    false and each has to come here and say so, which is the same currency
    discipline the section-count assertion above applies.
    """
    preamble = normalised(_dashboards_preamble())

    assert "Six" in preamble, (
        f"the preamble no longer states the count, which is what makes 'one of them built' "
        f"a fraction rather than a mood: {preamble[:200]!r}"
    )
    assert "`dashboards/provisioning/dashboards.yml`" in preamble, (
        "the preamble does not name the provisioning file, so it says the dashboards are "
        "provisioned without saying by what"
    )
    assert "bind mount" in preamble, (
        "the provisioning *mechanism* is unnamed — that the file is consumed by another "
        "repository's compose project is the asymmetry this whole arrangement rests on"
    )
    assert "`compose.yml` still gains nothing" in preamble, (
        "nothing records that Usher's own compose file is deliberately untouched, which is "
        "the half of the asymmetry an implementer is most likely to undo"
    )

    quoted = re.findall(r"`([^`]+)`", _dashboards_preamble())
    paths = [name for name in quoted if name.endswith((".json", ".yml", "/"))]
    assert paths, f"the preamble names no path at all, so nothing below can fail: {quoted}"

    # **The two kinds of path here are the asymmetry itself**, so they are separated
    # rather than filtered.
    external = [name for name in paths if name.startswith(("~", "/"))]
    internal = [name for name in paths if name not in external]

    assert external == ["~/code/observability/"], (
        "the preamble no longer names the external stack, so a reader has nowhere to look "
        f"for the compose project that mounts these files: {paths}"
    )
    assert internal, f"the preamble names no path inside this repository: {paths}"

    missing = [name for name in internal if not (_ROOT / name.rstrip("/")).exists()]
    assert missing == [], (
        f"the preamble names paths that do not exist in this tree: {missing} — which is "
        "precisely the defect the 2026-08-19 correction was written for"
    )


def test_the_preamble_scan_stops_above_the_first_dashboard_heading() -> None:
    """`_dashboards_preamble` truncating at the wrong place would let any of
    the six per-dashboard sections answer a claim about the set. Proved on a
    synthetic document, where the bait sits *below* the first heading."""
    document = (
        "## Dashboards\n\nSix, one built at `dashboards/x.json`.\n\n"
        "### 1 — Built\n\nProvisioned by a bind mount of `dashboards/decoy.yml`.\n\n"
        "## Where the stack lives\n\nElsewhere.\n"
    )

    preamble = _dashboards_preamble(document)

    assert "`dashboards/x.json`" in preamble
    assert "decoy" not in preamble, (
        "the preamble ran on into section 1, so a per-dashboard paragraph can satisfy a "
        f"claim about the whole set: {preamble!r}"
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


# --- The second check: a ⏳ marker that names a milestone which has shipped ---

_PROGRESS = _ROOT / "docs" / "plans" / "progress.md"

# The same anchor `test_docs_currency.py` uses, for the same reason: the
# document holds four status tables plus a `| M7 gets | From |` table 1,400
# lines further down, and an unanchored scan for `| M<n> |` reads all of them.
_MILESTONE_TABLE = "## Milestones (from"

# **`⏳ M<n>` is the marker; a bare `⏳` is not.** PRD 10 uses one in prose —
# *"**None of the three is marked ⏳** — that means *owed by a named
# milestone*"* — to name the vocabulary rather than to claim a debt, and a
# regex that matched the character alone would read that sentence as a marker
# owed by no milestone and have nothing to compare.
_OWED_MARKER = re.compile(r"⏳\s*(?P<milestone>M\d+)")

# Cells are split on **unescaped** pipes only. M10's status cell is one 6 kB
# paragraph carrying a Loki query — `{service_name="usher"} \|= "2fa839a2…"` —
# and `str.split("|")` turns that single row into seven cells, which silently
# moves the status into the wrong position rather than failing.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _milestone_status(progress: str) -> dict[str, str]:
    """`{"M9": "✅ complete on …", …}` from progress.md's milestone table."""
    start = progress.find(_MILESTONE_TABLE)
    assert start != -1, f"docs/plans/progress.md has no '{_MILESTONE_TABLE}…' heading"
    rest = progress[start + len(_MILESTONE_TABLE) :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest

    out: dict[str, str] = {}
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in _UNESCAPED_PIPE.split(line)]
        # `| name | milestone | plan | status |` splits to six, the first and
        # last empty. Fewer means a header rule or a narrower table.
        if len(cells) < 6:
            continue
        name, status = cells[1], cells[4]
        if re.fullmatch(r"M\d+", name):
            out[name] = status
    return out


def _owed_markers(text: str) -> list[tuple[int, str]]:
    """`(line number, milestone)` for every `⏳ M<n>` in the document."""
    return [
        (number, match["milestone"])
        for number, line in enumerate(text.splitlines(), start=1)
        for match in _OWED_MARKER.finditer(line)
    ]


def test_no_dashboard_panel_is_marked_owed_by_a_milestone_that_has_shipped() -> None:
    """⏳ means *owed by a named milestone*, so a ⏳ against a milestone that has
    shipped is a panel whose blocker is gone and whose document does not know
    it — the failure this file's neighbour checks for in the other direction.

    **Both parses can return nothing, and both are controlled.** An empty
    shipped set makes every marker read as legitimately owed, and an
    `_OWED_MARKER` that stopped matching makes a document full of stale markers
    read as clean. The status parse is controlled here on the real table, in
    both polarities — M9 shipped, M10 has not — and the marker parse is
    controlled on a synthetic document in the case below, because after this
    task the real file has no `⏳ M<n>` left to find. **That is why the
    positive control this task's own text specified — `assert milestones_seen`
    against the real document — is not written here: it would go red at exactly
    the moment the task succeeded.**

    The scan is over the whole document rather than over the dashboard sections
    alone. `⏳ M<n>` means the same thing wherever PRD 10 writes it, and a scan
    scoped to `## Dashboards` would inherit `_dashboards()`'s dependency on a
    heading for no gain in what it can catch.
    """
    status = _milestone_status(_PROGRESS.read_text(encoding="utf-8"))

    assert len(status) >= 10, (
        f"the milestone-table parse found {len(status)} rows ({sorted(status)}), so it is "
        "reading the wrong table or the wrong cell — with an empty shipped set every ⏳ "
        "marker below reads as legitimately owed"
    )
    assert status.get("M9", "").startswith("✅"), (
        f"M9 does not read as shipped in progress.md's milestone table: {status.get('M9')!r}"
    )
    assert not status.get("M10", "").startswith("✅"), (
        "M10 reads as shipped, so the status cell is being matched by something other than "
        f"its own marker: {status.get('M10')!r}"
    )

    shipped = {name for name, cell in status.items() if cell.startswith("✅")}

    stale = [
        f"{_PRD.name}:{number} — ⏳ {milestone}, but {milestone} is {status[milestone][:40]!r}"
        for number, milestone in _owed_markers(_PRD.read_text(encoding="utf-8"))
        if milestone in shipped
    ]

    assert stale == [], (
        "a dashboard panel is marked ⏳ against a milestone that has already shipped, so "
        "the document still calls a backed panel blocked: " + "; ".join(stale)
    )


def test_the_owed_marker_scan_separates_a_shipped_debt_from_a_live_one() -> None:
    """The marker parse's own control, on a synthetic document — the real one
    carries no `⏳ M<n>` once this task lands, so the case above cannot prove
    its scan still matches anything.

    Three lines, three distinct claims: a marker naming a shipped milestone is
    a finding, a marker naming an unshipped one is not, and the bare `⏳` PRD 10
    uses in prose to name the vocabulary is neither.
    """
    planted = (
        "Cost per play attributed to an LLM row stays ⏳ M9: it needs a client.\n"
        "A play-event log is a schema change and stays ⏳ M11.\n"
        "**None of the three is marked ⏳** — that means *owed by a named milestone*.\n"
    )

    found = _owed_markers(planted)

    assert found == [(1, "M9"), (2, "M11")], (
        f"the ⏳ scan did not read the planted markers as written: {found}"
    )
    assert [number for number, milestone in found if milestone == "M9"] == [1], (
        "the shipped filter did not select the stale marker alone"
    )


def test_the_milestone_status_parse_survives_an_escaped_pipe_in_the_status_cell() -> None:
    """M10's real row carries one `\\|`, inside a Loki query. It is the only
    escaped pipe in the table, and splitting on every pipe truncates that one
    cell rather than shifting it.

    **So the assertion is on the whole cell and not on its marker**, which is
    the correction this case needed: `\\|` sits *after* the `🚧`, so a prefix
    check reads the truncated cell as correct and the naive split survives it.
    Measured 2026-09-07 — with `_UNESCAPED_PIPE` replaced by `r"\\|"` the
    prefix form passed all eight cases in this module. The defect is
    unobservable in today's *conclusions* for exactly that reason; it stops
    being unobservable the first time a status cell puts an escaped pipe before
    its marker, and that row would then read as unshipped and forgive every ⏳
    against it.
    """
    table = (
        "## Milestones (from docs/specs/x.md)\n"
        "| # | Milestone | Plan file | Status |\n"
        "|---|---|---|---|\n"
        "| M9 | API surface | docs/plans/m9.md | ✅ complete |\n"
        '| M10 | Hardening | docs/plans/m10.md | 🚧 in progress, `{a="b"} \\|= "c"` |\n'
        "\n## Next\n"
    )

    status = _milestone_status(table)

    assert set(status) == {"M9", "M10"}, f"the table parse read {sorted(status)}"
    assert status["M9"] == "✅ complete"
    assert status["M10"] == '🚧 in progress, `{a="b"} \\|= "c"`', (
        f"the escaped pipe truncated M10's status cell, which read as {status['M10']!r}"
    )


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


def _dashboard_five_panels() -> list[dict[str, Any]]:
    """Every panel of the committed compliance dashboard, rows flattened."""
    return panels(json.loads(_DASHBOARD_FIVE.read_text(encoding="utf-8")))


def committed_compliance_sql() -> list[str]:
    """The committed targets that read the TMDb payload cache, as SQL.

    Selected by what they *query* -- `raw_payloads` filtered to `'tmdb'` --
    rather than by panel title, because a title is prose and this is the block
    TMDb's retention term is enforced by.
    """
    found: list[str] = []
    for panel in _dashboard_five_panels():
        for target in panel.get("targets") or []:
            sql = str(target.get("rawSql", ""))
            if "raw_payloads" in sql and "'tmdb'" in sql:
                found.append(sql)
    return found


# `interval '6 months'`, whatever the term. Captured rather than matched so the
# *set of terms the dashboard reads* is what gets asserted.
_INTERVAL = re.compile(r"interval\s+'(?P<term>[^']+)'")


def test_the_committed_compliance_panels_read_the_term_the_prd_states() -> None:
    """🔴 The retention half of TMDb's licence has exactly one enforcement in this
    repository, and it is the committed JSON rather than the PRD.
    """
    statements = committed_compliance_sql()

    assert len(statements) == 3, (
        f"the committed dashboard reads the TMDb cache in {len(statements)} targets, not "
        "the three PRD 10 spells out -- zero means this scan globs nothing and every "
        "assertion below is vacuous, and any other number means the transcription has "
        "diverged from the block test_raw_payload_cache_age.py executes"
    )

    ceilingless = [statement for statement in statements if not _INTERVAL.search(statement)]
    assert ceilingless == [], (
        f"these committed targets read the TMDb cache against no interval at all, so they "
        f"answer a question with no ceiling in it: {ceilingless}"
    )

    committed = {
        match["term"] for statement in statements for match in _INTERVAL.finditer(statement)
    }
    stated = {
        match["term"]
        for statement in compliance_panel_sql()
        for match in _INTERVAL.finditer(statement)
    }

    assert committed == {"6 months"}, (
        f"the committed compliance panels read {sorted(committed)} -- TMDb's term is no "
        "more than six months, and a widened ceiling is a licence breach this dashboard "
        "would then report as zero"
    )
    assert committed == stated, (
        f"the dashboard reads {sorted(committed)} and PRD 10's block states "
        f"{sorted(stated)}; the two transcriptions of one ceiling have drifted apart and "
        "only one of them is deployed"
    )


def test_the_committed_compliance_scan_is_falsifiable() -> None:
    """The scan above is a substring selection over JSON, and a selection that
    matched nothing would make every assertion in it pass on an empty set --
    except the count, which is why the count is asserted first. This is the
    other half: the term extractor answers what a widened ceiling looks like,
    proved on a statement rather than on the committed file."""
    widened = "SELECT count(*) FROM raw_payloads WHERE fetched_at < now() - interval '12 months'"

    assert [match["term"] for match in _INTERVAL.finditer(widened)] == ["12 months"]
    assert _INTERVAL.search("SELECT count(*) FROM raw_payloads") is None


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
