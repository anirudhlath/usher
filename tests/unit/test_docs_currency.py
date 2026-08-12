"""Two documentation status tables drift, and the drift has been measured
twice — so this milestone fixes it with a test rather than with attention.

`docs/plans/progress.md`'s milestone table said "IN PROGRESS" for a milestone
merged four months earlier, and its own note calls that *"the most-read wrong
statement in the repository"*. `docs/prd/README.md`'s implementation-plan table
stopped at M6 and was missing M7's row until M8 added it by hand. Same failure,
two documents, nobody's job — which is exactly the shape a `pytest` case is for.

**The scoping is the whole design, and it is what stops this check being
satisfied by prose.** M9's H2 measured the trap directly one task earlier: its
own conformance check read a PRD document whole, its repair shipped with a
blockquote spelling the corrected path, and **re-planting the defect then
satisfied the check on the defect's behalf**. A documentation check that reads
prose can be answered by prose written to explain the fix.

`docs/plans/progress.md` is 3,000 lines and names a plan file in **six** prose
headings of the form `## M8 plan: docs/plans/2026-08-06-m8-curation.md`, one per
milestone that has run. That is this document's habit, not an accident, so the
next milestone to write such a heading would turn a whole-document scan green
while its table row stayed missing — which is the drift, restored, with the
check reporting success. Both tables are therefore harvested from **table rows
under a named heading**, and
`test_a_plan_named_only_in_prose_does_not_satisfy_the_table` proves the scoping
against a document that carries both spellings.
"""

import pathlib
import re

_ROOT = pathlib.Path(__file__).parents[2]
_PLANS = _ROOT / "docs" / "plans"
_PROGRESS = _PLANS / "progress.md"
_PRD_README = _ROOT / "docs" / "prd" / "README.md"

# A floor rather than an equality, on this file's neighbour's precedent
# (`test_decision_register.py` asserts `>= 23` against 35 ADRs that exist). Nine
# plan files exist at M9's close, one per milestone M1..M9. A floor grows with
# the project; an equality is a line the next milestone edits, which is how a
# count stops being a measurement and becomes a number people bump until green.
PLAN_FILES_AT_M9_CLOSE = 9

_MILESTONE_TABLE = "## Milestones (from"
_IMPLEMENTATION_PLAN_TABLE = "## Implementation plans"

# `2026-08-06-m8-curation.md`. Deliberately narrow: the milestone table's own
# heading names `docs/specs/2026-07-28-usher-v1-design.md`, which is a spec and
# not a plan, and must not be harvested as one.
_PLAN_FILENAME = re.compile(r"20\d\d-\d\d-\d\d-m\d+-[a-z0-9-]+\.md")


def _section(document: str, heading: str) -> list[str]:
    """The lines under a level-2 heading, up to the next level-2 heading.

    The heading line itself is excluded, because the milestone table's heading
    carries a `docs/specs/…` path and a section that included it would be
    harvesting its own title.
    """
    lines = document.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
    assert len(starts) == 1, f"expected exactly one {heading!r} heading, found {len(starts)}"
    start = starts[0] + 1
    for offset, line in enumerate(lines[start:]):
        if line.startswith("## "):
            return lines[start : start + offset]
    return lines[start:]


def _table_rows(document: str, heading: str) -> set[str]:
    """Every plan filename named by a **table row** under `heading`."""
    return {
        name
        for line in _section(document, heading)
        if line.startswith("|")
        for name in _PLAN_FILENAME.findall(line)
    }


def test_every_plan_file_is_named_by_both_status_tables() -> None:
    """Kills adding a milestone's plan and leaving either status table behind,
    which has happened twice and was repaired by hand both times.

    Checked in both directions. A row pointing at a file that does not exist is
    the same defect wearing the other face -- a renamed plan leaves a link that
    resolves nowhere, and the PRD link check does not cover `docs/plans/`
    (`.claude/rules/prd-maintenance.md` records why that exclusion is a
    correction rather than a convenience).
    """
    on_disk = {path.name for path in _PLANS.glob("*.md")} - {"progress.md"}

    assert len(on_disk) >= PLAN_FILES_AT_M9_CLOSE, (
        f"the plan-file scan found only {len(on_disk)}, so it is walking the wrong directory"
    )
    assert "2026-07-28-m1-foundation.md" in on_disk, (
        "the plan-file scan ran but found no M1 plan, so it is not reading docs/plans/"
    )

    tables = {
        "docs/plans/progress.md's milestone table": _table_rows(
            _PROGRESS.read_text(), _MILESTONE_TABLE
        ),
        "docs/prd/README.md's implementation-plan table": _table_rows(
            _PRD_README.read_text(), _IMPLEMENTATION_PLAN_TABLE
        ),
    }

    for where, named in tables.items():
        assert not on_disk - named, f"{where} does not name: {sorted(on_disk - named)}"
        assert not named - on_disk, (
            f"{where} names a plan file that does not exist: {sorted(named - on_disk)}"
        )


def test_a_plan_named_only_in_prose_does_not_satisfy_the_table() -> None:
    """The scoping above, asserted rather than described -- because H2 measured
    a documentation check being satisfied by the prose that explained its own
    repair, and a check that reads a whole document is the same defect waiting.

    The document below carries both spellings of the same plan file: a table row
    for M1, and a prose heading plus a sentence for M9. A whole-document scan
    answers `{M1, M9}` and reports the table as complete; the scoped extraction
    answers `{M1}` and reports M9 missing, which is the truth.

    The second assertion is the premise. Without it the first is satisfied by a
    regex that cannot see the M9 filename at all -- for the same reason every
    scan in this repository carries a non-emptiness control.
    """
    document = (
        "## Milestones (from docs/specs/2026-07-28-usher-v1-design.md)\n"
        "| # | Milestone | Plan file | Status |\n"
        "|---|---|---|---|\n"
        "| M1 | Foundation | docs/plans/2026-07-28-m1-foundation.md | done |\n"
        "\n"
        "## M9 plan: docs/plans/2026-08-10-m9-api-surface.md (74 tasks)\n"
        "\n"
        "Its 74 tasks live in `docs/plans/2026-08-10-m9-api-surface.md`.\n"
    )

    assert _table_rows(document, _MILESTONE_TABLE) == {"2026-07-28-m1-foundation.md"}
    assert set(_PLAN_FILENAME.findall(document)) == {
        "2026-07-28-m1-foundation.md",
        "2026-08-10-m9-api-surface.md",
    }, "the premise: the prose really does name a second plan file"


def test_the_progress_log_really_does_name_plan_files_outside_its_table() -> None:
    """The premise the case above is modelled on, stated against the real
    document so the model is not a hypothetical.

    Measured at M9's close: `docs/plans/progress.md` names a plan file on **6**
    lines outside its milestone table -- one prose heading per milestone that
    has run, plus a line inside M2's fixture-leak note. Every one of those is a
    line a whole-document scan would count as a table row.

    Asserted as a floor and not as an equality: the number grows by one per
    milestone, and the claim being kept alive is *"this document's prose names
    plan files"*, not *"it names exactly six"*.
    """
    text = _PROGRESS.read_text()
    section = set(_section(text, _MILESTONE_TABLE))
    outside = [
        line
        for line in text.splitlines()
        if line not in section and _PLAN_FILENAME.search(line) is not None
    ]

    assert len(outside) >= 6, (
        "progress.md no longer names a plan file outside its milestone table, so "
        f"the scoping above is no longer load-bearing: found {outside!r}"
    )
