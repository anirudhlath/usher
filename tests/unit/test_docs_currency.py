"""Two documentation status tables drift, and the drift has been measured twice — so
this milestone fixes it with a test rather than with attention.
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
# `2026-08-19-rating-provenance-split.md`, `2026-08-21-issue-41-resumable-watch-
# lane.md`.
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
    """Kills adding a milestone's plan and leaving either status table behind, which has
    happened twice and was repaired by hand both times.
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

    # All five, and the second is the one with teeth: `m9` is letters-then-digits, so
    # the *old* pattern harvested that spec as a plan and the exclusion was never as
    # complete as its comment claimed.
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
    """The premise the case above is modelled on, stated against the real document so the
    model is not a hypothetical.
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


# The import-contract count is written down in three places and derived in none:
# `pyproject.toml` defines the contracts, `docs/prd/01-architecture.md` states how many
# exist, and `CLAUDE.md`'s gate block annotates `uv run lint-imports` with what a green
# run prints.
_ARCHITECTURE = _ROOT / "docs" / "prd" / "01-architecture.md"
_PYPROJECT = _ROOT / "pyproject.toml"
_CLAUDE_MD = _ROOT / "CLAUDE.md"

_CONTRACT_HEADER = re.compile(r"^\[\[tool\.importlinter\.contracts\]\]", re.MULTILINE)
_PRD_CONTRACT_COUNT = re.compile(r"\*\*(\w+) contracts\b", re.IGNORECASE)
_GATE_CONTRACT_COUNT = re.compile(r"architecture contracts — (\d+) kept, (\d+) broken")

_NUMBER_WORDS = {
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
}


def _defined_contract_count() -> int:
    return len(_CONTRACT_HEADER.findall(_PYPROJECT.read_text(encoding="utf-8")))


def test_the_architecture_prd_states_the_number_of_contracts_that_exist() -> None:
    defined = _defined_contract_count()
    assert defined >= 8, (
        "fewer contracts are defined than the PRD's narrative describes, so "
        f"this test is measuring the wrong table: found {defined}"
    )

    match = _PRD_CONTRACT_COUNT.search(_ARCHITECTURE.read_text(encoding="utf-8"))
    assert match is not None, (
        "docs/prd/01-architecture.md no longer states a bolded contract count, "
        "so the claim this test was written to pin has been reworded away"
    )

    word = match.group(1).lower()
    assert word in _NUMBER_WORDS, (
        f"docs/prd/01-architecture.md says {word!r} contracts, which is not a "
        f"number word this test can read; add it to _NUMBER_WORDS"
    )
    assert _NUMBER_WORDS[word] == defined, (
        f"docs/prd/01-architecture.md says {word!r} ({_NUMBER_WORDS[word]}) "
        f"import contracts, pyproject.toml defines {defined}"
    )


def test_the_gate_block_annotates_lint_imports_with_the_real_contract_count() -> None:
    defined = _defined_contract_count()

    match = _GATE_CONTRACT_COUNT.search(_CLAUDE_MD.read_text(encoding="utf-8"))
    assert match is not None, (
        "CLAUDE.md's gate block no longer annotates `uv run lint-imports` with "
        "a kept/broken count, so the annotation this test pins is gone"
    )

    kept, broken = int(match.group(1)), int(match.group(2))
    assert (kept, broken) == (defined, 0), (
        f"CLAUDE.md's gate says {kept} kept / {broken} broken, "
        f"pyproject.toml defines {defined} contracts"
    )
