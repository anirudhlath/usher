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

`docs/plans/progress.md` is 3,000 lines and names a plan file in **five** prose
headings of the form `## M8 plan: docs/plans/2026-08-06-m8-curation.md` (M2, M3,
M4, M5, M8), plus M1's `Plan file:` line and M2's fixture-leak note -- seven
lines of prose in all. That is this document's habit, not an accident, so the
next milestone to write such a heading would turn a whole-document scan green
while its table row stayed missing — which is the drift, restored, with the
check reporting success. Both tables are therefore harvested from **table rows
under a named heading**, and
`test_a_plan_named_only_in_prose_does_not_satisfy_the_table` proves the scoping
against a document that carries both spellings.

**`docs/plans/progress.md` now carries four such headings and is read as
their union**, because the first plan that is not a milestone arrived on
2026-08-18: E1 is phase 1 of 4 of
`docs/specs/2026-08-18-usher-quality-evals-design.md`, and the milestone
table's heading names the *other* spec. Registering E1 there would have made
that heading false in order to make this file green -- so the scope segment of
`_PLAN_FILENAME` was widened from `m\\d+` to `[a-z]+\\d+`, and a second heading
was added naming its own spec.

**The third heading arrived on 2026-08-19 and the same widening had to happen
again, for a plan that carries no scope segment at all.** The rating-provenance
split is a repair rather than a stage of anything, so it is neither `m9` nor
`e1`, and `[a-z]+\\d+` could not see `2026-08-19-rating-provenance-split.md` --
the plan was committed, unregistered, and **the check was already red at
`2651388`**, before the task that found it changed a line. The pattern is now a
general dated plan name with the spec exclusion carried explicitly rather than
by narrowness. Every widening and the exclusion it preserves are asserted by
`test_the_filename_pattern_harvests_an_eval_phase_and_still_refuses_a_spec`.

**The fourth heading landed on 2026-08-25 and cost the pattern nothing.**
`2026-08-21-issue-41-resumable-watch-lane.md` -- authored 2026-08-21, which is
the date in its filename -- is a bug fix rather than a milestone, a phase or a
repair of a column, and it has its own spec, so it needed a heading of its own
by the same argument E1 and the rating split each needed one. Its harvest was
*confirmed* rather than assumed, and the confirmation is worth no more than
that: any dated lowercase-hyphen plan name is harvested by construction now, so
a third plan being seen is the pattern doing what it says, not evidence about
its generality. The two widenings are still the only measurements here.

**Four headings against five specs, and the gap is by design.** A *milestone*
of the v1 design stays in the milestone table even after it gets its own
detailed spec -- `docs/specs/2026-08-10-m9-api-surface-design.md` has no heading
because M9 is a milestone, and the milestone heading naming the v1 design is
true of it. The per-spec-heading rule is for plans that are **not** milestones:
those have no honest home under an existing heading, so they get their own.
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
_EVAL_PHASE_TABLE = "## Quality-eval phases (from"
_RATING_SPLIT_TABLE = "## Rating provenance (from"
_WATCH_RESUME_TABLE = "## Resumable watch lane (from"
_IMPLEMENTATION_PLAN_TABLE = "## Implementation plans"

# `2026-08-06-m8-curation.md`, `2026-08-18-e1-eval-skeleton-and-suggest.md`,
# `2026-08-19-rating-provenance-split.md`,
# `2026-08-21-issue-41-resumable-watch-lane.md`.
#
# The scope segment was `m\d+` until E1 and `[a-z]+\d+` until the rating split,
# and each narrowing was invisible to a check whose whole subject is plans
# nobody registered: E1 is a *phase* rather than a milestone, and
# `rating-provenance-split` carries **no scope segment at all** -- it is neither
# numbered nor lettered, because it is a repair rather than a stage of
# anything. It was on disk, unregistered, and harvested by nothing.
#
# **What every widening has had to keep is the spec exclusion**, because every
# status table names its own spec in its own heading and a PRD row links one
# inside a table cell. The narrowness cannot carry that any more, so it is
# now carried explicitly: all **five** specs this project has written end
# `-design.md` (`2026-07-28-usher-v1-design.md`,
# `2026-08-10-m9-api-surface-design.md`,
# `2026-08-18-usher-quality-evals-design.md`,
# `2026-08-19-rating-provenance-split-design.md`,
# `2026-08-21-issue-41-resumable-watch-lane-design.md`), and the lookahead
# refuses exactly those.
#
# ⚠️ **The second of those five is the one that matters, and an earlier
# spelling of this comment omitted it while claiming to enumerate them all.**
# `2026-08-10-m9-api-surface-design.md` is the single spec the *old*
# `[a-z]+\d+` pattern did **not** exclude -- `m9` is letters-then-digits and
# `-api-surface-design.md` follows it -- so a narrowness described as
# refusing every spec was in fact harvesting one of them as a plan. Measured:
# it was being counted as a line of prose naming a plan file by the floor case
# below, which is why that floor's number moves in this commit.
#
# The lookahead is spelled `[a-z0-9-]*` rather than `.*` deliberately: a greedy
# `.*` would reach a *later* `-design.md` on the same line and refuse the plan
# named beside its own spec, which is precisely the row the rating split's
# table carries.
# `test_the_filename_pattern_harvests_an_eval_phase_and_still_refuses_a_spec`
# asserts every half.
_PLAN_FILENAME = re.compile(r"20\d\d-\d\d-\d\d-(?![a-z0-9-]*-design\.md)[a-z0-9-]+\.md")


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


def test_every_plan_file_is_named_by_every_status_table() -> None:
    """Kills adding a milestone's plan and leaving either status table behind,
    which has happened twice and was repaired by hand both times.

    Checked in both directions. A row pointing at a file that does not exist is
    the same defect wearing the other face -- a renamed plan leaves a link that
    resolves nowhere, and the PRD link check does not cover `docs/plans/`
    (`.claude/rules/prd-maintenance.md` records why that exclusion is a
    correction rather than a convenience).

    `docs/plans/progress.md` is read as the **union of its four tables**, one
    per spec. The alternative -- an E1 row under a heading that says
    *"Milestones (from docs/specs/2026-07-28-usher-v1-design.md)"* -- turns that
    heading into a false statement to satisfy a check about documentation being
    true, which is the trade this whole module exists to refuse. A plan that is
    **not a milestone** is registered under a heading naming its own spec, and
    the union is what keeps the obligation *"every plan file is named"* one
    obligation. Four headings against five specs is that rule and not a lapse:
    M9's plan sits under the milestone heading because M9 *is* a milestone of
    the v1 design that heading names, so no false statement is being made on
    its behalf even though `2026-08-10-m9-api-surface-design.md` exists.
    """
    on_disk = {path.name for path in _PLANS.glob("*.md")} - {"progress.md"}

    assert len(on_disk) >= PLAN_FILES_AT_M9_CLOSE, (
        f"the plan-file scan found only {len(on_disk)}, so it is walking the wrong directory"
    )
    assert "2026-07-28-m1-foundation.md" in on_disk, (
        "the plan-file scan ran but found no M1 plan, so it is not reading docs/plans/"
    )

    progress = _PROGRESS.read_text()
    tables = {
        "docs/plans/progress.md's four status tables": (
            _table_rows(progress, _MILESTONE_TABLE)
            | _table_rows(progress, _EVAL_PHASE_TABLE)
            | _table_rows(progress, _RATING_SPLIT_TABLE)
            | _table_rows(progress, _WATCH_RESUME_TABLE)
        ),
        "docs/prd/README.md's implementation-plan table": _table_rows(
            _PRD_README.read_text(), _IMPLEMENTATION_PLAN_TABLE
        ),
    }

    for where, named in tables.items():
        assert not on_disk - named, (
            f"{where} does not name: {sorted(on_disk - named)}. If this plan belongs "
            "to a spec with no heading yet, give it its own heading and table naming "
            "that spec -- a row under an existing heading makes that heading false, "
            "which is the trade this module exists to refuse. A plan that really is a "
            "milestone of the v1 design belongs in the milestone table."
        )
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


def test_the_filename_pattern_harvests_an_eval_phase_and_still_refuses_a_spec() -> None:
    """Pins the widening that let `E1` in, because an unpinned widening is the
    same defect as an unregistered plan: both are a check that reports success
    over a file it cannot see.

    `2026-08-18-e1-eval-skeleton-and-suggest.md` is a *phase* of a second spec,
    not a milestone of the first, and the pattern was `-m\\d+-` until this case
    existed -- so the plan was on disk, its row was in a table, and the harvest
    of that row was the empty set. The first assertion is the widening.

    The second is what the widening had to keep. `_PLAN_FILENAME` is narrow so
    that a `docs/specs/…` path is not harvested as a plan, and every status
    table names its own spec in its own heading. A pattern loose enough to
    read `usher-v1-design` would make the milestone table's title one of its
    own rows, so the exclusion is asserted where a harvest actually happens --
    inside a table row -- rather than against the heading, which
    `_table_rows` skips for a different reason and would pass either way.
    """
    document = (
        "## Quality-eval phases (from docs/specs/2026-08-18-usher-quality-evals-design.md)\n"
        "| Phase | What it delivers | Plan file | Status |\n"
        "|---|---|---|---|\n"
        "| E1 | Skeleton | docs/plans/2026-08-18-e1-eval-skeleton-and-suggest.md | in progress |\n"
    )

    assert _table_rows(document, _EVAL_PHASE_TABLE) == {
        "2026-08-18-e1-eval-skeleton-and-suggest.md"
    }

    # All five, and the second is the one with teeth: `m9` is
    # letters-then-digits, so the *old* pattern harvested that spec as a plan
    # and the exclusion was never as complete as its comment claimed. The
    # fifth was added on 2026-08-25 rather than assumed: a spec arriving with
    # a fourth status table is exactly when "the lookahead already refuses it"
    # is a prediction somebody should check, and it costs one line to.
    #
    # Five specs, four headings -- M9's is the one with no heading of its own,
    # because M9 is a milestone and the milestone heading names the v1 design.
    specs = (
        "## Quality-eval phases (from docs/specs/2026-08-18-usher-quality-evals-design.md)\n"
        "| — | — | docs/specs/2026-07-28-usher-v1-design.md | — |\n"
        "| — | — | docs/specs/2026-08-10-m9-api-surface-design.md | — |\n"
        "| — | — | docs/specs/2026-08-18-usher-quality-evals-design.md | — |\n"
        "| — | — | docs/specs/2026-08-19-rating-provenance-split-design.md | — |\n"
        "| — | — | docs/specs/2026-08-21-issue-41-resumable-watch-lane-design.md | — |\n"
    )

    assert _table_rows(specs, _EVAL_PHASE_TABLE) == set(), (
        "a spec is not a plan, and four of these five are named by a heading"
    )

    # **The second widening, and the row that forced it.** The rating split
    # carries no scope segment -- not `m9`, not `e1` -- so `[a-z]+\d+` was
    # blind to it, and its own row names its spec in the same cell as the plan.
    # A greedy `.*` in the exclusion would reach that spec and refuse the plan
    # standing beside it, which is why the lookahead is bounded to the filename.
    unnumbered = (
        "## Rating provenance (from docs/specs/2026-08-19-rating-provenance-split-design.md)\n"
        "| Task | Plan file | Spec | Status |\n"
        "|---|---|---|---|\n"
        "| 1 | docs/plans/2026-08-19-rating-provenance-split.md | "
        "docs/specs/2026-08-19-rating-provenance-split-design.md | done |\n"
    )

    assert _table_rows(unnumbered, _RATING_SPLIT_TABLE) == {
        "2026-08-19-rating-provenance-split.md"
    }, "the plan is harvested and the spec beside it on the same line is not"


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

    **All four** table sections are excluded, and every one after the first is
    why this paragraph exists. `## Quality-eval phases` arrived on 2026-08-18,
    `## Rating provenance` on 2026-08-19 and `## Resumable watch lane` on
    2026-08-25, and the rows of all three are outside the *milestone* section --
    so a scan subtracting only that one would have counted a table row as a
    line of prose, and the floor would have kept passing, on evidence it is
    written to exclude. A floor that goes green because the thing it measures
    was diluted is worse than one that fails: it reports that the scoping is
    still load-bearing while quietly measuring something else.

    **The count is 7, and it moved from 8 by getting more honest rather than
    by anything being deleted.** The eighth line was
    `docs/specs/2026-08-10-m9-api-surface-design.md`, harvested as a plan
    because the old `[a-z]+\\d+` scope segment matched `m9` -- so *"all eight
    genuinely prose"* was already one short of true when it was written, and
    the widening that let `2026-08-19-rating-provenance-split.md` in is what
    exposed it. All seven remaining are genuinely prose: **five** per-milestone
    headings (M2, M3, M4, M5, M8), M1's `Plan file:` line, and M2's
    fixture-leak note. Asserted as a floor and not an equality for the reason
    above -- the number grows by one per milestone.

    **The subtraction here and the union in the case above should move
    together, and only one of them can tell you when they haven't** -- which is
    why the fourth table's registration is two edits in two functions rather
    than one. Measured on 2026-08-25, when it landed: subtracting three
    sections instead of four counts **8** lines here rather than 7, because the
    new table's own row is then read as prose. That is a *different* eight from
    the paragraph above -- this one is a table row miscounted today, that one
    was a spec miscounted until 2026-08-19 -- and the thing they have in common
    is the reason both are written down: `>= 6` passes on every one of these
    numbers, so neither dilution could ever announce itself through the count.

    **So the count is not what notices, and the second assertion below is.** A
    status-table row starts with `|` and a line of prose does not, so a pipe
    line in `outside` means a section is missing from the subtraction -- a
    shape rather than a number, with nothing to bump when the next table lands.
    Measured: zero pipe lines among the current 7, and dropping any one section
    from `tabled` turns it red naming the row it wrongly counted. **The caveat,
    because it is a real one:** a future table *elsewhere* in this document
    legitimately naming a plan file in a row would false-red here and have to
    be added to `tabled`. That is the direction this repository prefers -- a
    red that names the row beats a floor that absorbs it in silence.
    """
    text = _PROGRESS.read_text()
    tabled = (
        set(_section(text, _MILESTONE_TABLE))
        | set(_section(text, _EVAL_PHASE_TABLE))
        | set(_section(text, _RATING_SPLIT_TABLE))
        | set(_section(text, _WATCH_RESUME_TABLE))
    )
    outside = [
        line
        for line in text.splitlines()
        if line not in tabled and _PLAN_FILENAME.search(line) is not None
    ]

    assert len(outside) >= 6, (
        "progress.md no longer names a plan file outside its status tables, so "
        f"the scoping above is no longer load-bearing: found {outside!r}"
    )

    rows = [line for line in outside if line.startswith("|")]
    assert not rows, (
        "a status-table row is being counted as prose, so a section is missing "
        f"from the subtraction above: {rows}"
    )
