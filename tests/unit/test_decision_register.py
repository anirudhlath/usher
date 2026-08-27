"""The ADR register is hand-maintained and nothing checked it until this
file existed.

Same argument M6 made for `ALL_PORTS`: twenty-two rows kept correct by
attention rather than by construction. An ADR that is written and not
listed is a document nobody finds, and the failure is silent in both
directions -- a missing row, or a row pointing at a file that was renamed.
"""

import pathlib
import re
from datetime import datetime

_DECISIONS = pathlib.Path(__file__).parents[2] / "docs" / "prd" / "decisions"


def test_every_adr_file_is_listed_in_the_decisions_register() -> None:
    """Kills adding an ADR and forgetting the register row -- which is
    exactly what this task would do if the row were a checklist item
    instead of a test.

    The control assertions are not decoration. `tests/unit/test_no_third_
    party_data.py` carries the same pair for the same reason M6's Task 2
    found: a scan that globs nothing passes identically to a scan that
    passes, so an empty glob has to be a failure on its own.

    Verified against today's tree before 0023 existed, with the floor
    relaxed to 22: twenty-two files, twenty-two rows, both directions
    clean. So this case starts from a register that was in fact correct,
    rather than from one it would have to be loosened to accommodate.

    **The floor moved 23 -> 35 once, at M9's close, and the timing is the
    point.** M9 wrote six ADRs (0030-0035) across five groups, and a floor
    each of them bumped would have been five merge conflicts about a
    non-emptiness control -- so the milestone's documentation-reconciliation
    task moves it exactly once, at the last moment an ADR can land, to the
    count measured on the tree. It stays a **floor** rather than an equality
    for the reason the register itself exists: an equality is a number people
    raise until the suite is green, and what this assertion is for is proving
    the glob found something, not counting ADRs. The two set comparisons below
    are what actually check the register, in both directions.
    """
    register = (_DECISIONS / "README.md").read_text()
    files = {path.name for path in _DECISIONS.glob("0*.md")}

    assert len(files) >= 35, f"the register scan found only {len(files)} ADRs"
    assert "0001-abc-over-protocol.md" in files

    linked = set(re.findall(r"\]\((0\d{3}-[a-z0-9-]+\.md)\)", register))
    assert files - linked == set(), f"ADRs missing from the register: {sorted(files - linked)}"
    assert linked - files == set(), f"register rows pointing at nothing: {sorted(linked - files)}"


def test_no_two_adrs_claim_the_same_number() -> None:
    """**The register check above cannot see this one, and 2026-08-20 is how we
    found that out.** `spec/quality-evals` wrote `0039-the-eval-schema-is-not-a-
    migration.md` while `main` merged `0039-the-genre-vocabulary-is-usher-
    owned.md`. Two *different filenames*, so git resolves no conflict and a
    merge simply keeps both — and every one of the 14 bare `ADR-0039`
    references on one side and 16 on the other silently stops naming one
    document.

    It passes `test_every_adr_file_is_listed_in_the_decisions_register` too:
    both files exist, both get a row, both directions of that set comparison
    are clean. A register can be complete and still ambiguous, which is why
    this is a second case rather than a third assertion in that one.

    The check only fires once both files are in one tree — i.e. after the
    merge, not before it. That is the whole point: the merge is the moment the
    collision becomes real and the moment nothing else reports it.

    Same non-emptiness control as its neighbour, and for the same reason: a
    glob that finds nothing has no duplicates either.

    **This case was written twice, independently, and that is the finding.**
    `spec/quality-evals` wrote it under this name and `milestone/m10-hardening`
    wrote `test_no_two_decision_records_claim_the_same_number` — same glob, same
    prefix slice, same duplicate check, neither branch able to see the other
    doing it. The 2026-08-21 merge is where they met, and folding them into one
    case is the whole of that resolution.

    **And on the same merge the collision recurred, at two numbers at once**,
    which retires the sentence the branch's copy ended on (*"the limiter moved
    to 0040; this is what stops the next parallel branch re-doing it"*). It did
    not stop it. `main` minted `0040-rating-columns-name-their-source.md` and
    `0041-the-eval-schema-is-not-a-migration.md`; the branch, having already
    moved its limiter *to* 0040, held `0040-the-outbound-limiter-...` and
    `0041-a-bounded-column-...`. Both branches were green in isolation and the
    merge was red on two numbers.

    **The mechanism is the allocation rule, not the guard.** "Take the next free
    number" computed against *your own* tree is not a reservation, so two
    branches that never see each other compute the same answer — and a renumber
    that also takes the next free number is exposed to the identical race, which
    is precisely how the branch's 0039 → 0040 repair walked into main's 0040.
    This case is a *detector*, and a detector that fires at merge time cannot
    prevent what it detects; the branch ADRs moved to 0042 and 0043 on
    2026-08-21. Anything that actually fixes this has to make the number
    unguessable-in-parallel or reserved per branch, and that is not decided
    here.

    **A fourth collision, 2026-08-26, at 0042 — and this case is the only thing
    that reported it.** `main` merged issue #41's
    `0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md` while
    `milestone/m10-hardening` held `0042-the-outbound-limiter-...`; two
    filenames again, no git conflict again, both files in the tree again. It
    resolved as a *cascade* rather than a swap — `main`'s highest number is
    0042, so the branch's three records each moved up one, 0042 → 0043 → 0044
    → 0045, highest-first so that no two files claimed one number on disk at
    any point. The numbers taken were checked free on **both** sides first,
    which is the step the 0039 → 0040 repair skipped.

    Four collisions in seven days, all on the same allocation rule, is the
    argument the paragraph above makes; what this one adds is that the rule
    survives a renumber *cascade* too, because a cascade still has to guess.
    """
    numbers = [path.name[:4] for path in _DECISIONS.glob("0*.md")]

    assert len(numbers) >= 35, f"the register scan found only {len(numbers)} ADRs"

    duplicated = sorted({number for number in numbers if numbers.count(number) > 1})
    assert duplicated == [], (
        f"these ADR numbers are claimed by more than one file: {duplicated} — "
        f"a merge that kept both sides of a number collision, which resolves "
        f"no conflict because the filenames differ"
    )


def test_the_provider_proposal_adr_is_reachable_from_prd_06() -> None:
    """An ADR the PRD does not link is one the next person composing rows
    will not read, and this is the decision they are most likely to
    re-litigate -- because the alternative is shorter and PRD 06's own
    "drops any that build empty" reads like an endorsement of it.

    Kills writing the ADR and leaving PRD 06's composition paragraph
    unchanged.
    """
    prd = (_DECISIONS.parent / "06-rows-and-recommendations.md").read_text()
    assert "0023-a-provider-proposes-it-does-not-decide.md" in prd


def test_the_playback_ticket_adr_is_reachable_from_prd_07_and_from_adr_0012() -> None:
    """ADR-0029 settles ADR-0012's named M9 successor -- ADR-0012's own
    "The successor, in M9" section named two options and deferred the
    choice. A reader who reaches PRD 07's Playback section but not
    ADR-0012, or ADR-0012 but not ADR-0029, is a reader who re-derives which
    option was actually built and re-litigates the "removes the credential"
    mistake ADR-0029 exists to correct.

    Kills writing ADR-0029 and leaving either link unwritten -- PRD 07's
    Playback section (H's D4) or ADR-0012's own Status line and successor
    section (this task).
    """
    prd = (_DECISIONS.parent / "07-client-api.md").read_text()
    adr_0012 = (_DECISIONS / "0012-playback-urls-carry-a-source-token.md").read_text()

    target = "0029-the-playback-ticket-changes-the-artifact-not-the-grant.md"
    assert target in prd, "PRD 07's Playback section does not link ADR-0029"
    assert target in adr_0012, "ADR-0012 does not point at its own settled successor"


def test_the_two_tier_suggest_adr_is_reachable_from_prd_05_and_from_adr_0002() -> None:
    """ADR-0031 discharges the follow-up ADR-0002's failed typo-tolerance gate
    opened, and the two documents disagree with each other unless both links
    exist.

    ADR-0002's consequence 2 says the btree probe is *"the only thing measured
    that fits inside a keystroke"*, on a figure taken over whole mutated names.
    ADR-0031 narrows exactly that sentence: at a one-character prefix the same
    statement is 291 ms on `titles` alone and 2,707 ms over the union. **A
    reader who reaches ADR-0002 and not ADR-0031 therefore comes away with a
    claim this project has measured to be wrong at the short end** — which is
    worse than an unlinked ADR generally is, and is why the link is asserted
    rather than left to a register row.

    The same holds one document over: PRD 05's autocomplete section carries the
    two-tier prescription and the per-length curve, and a minimum prefix length
    is the request-boundary decision that section explicitly defers to the ADR.

    Kills writing ADR-0031 and leaving either link unwritten. Deliberately not
    scoped to PRD 07 as well: that document's `GET /search/suggest` row names
    the route, and the argument lives with the search subsystem.
    """
    prd_05 = (_DECISIONS.parent / "05-search-and-similarity.md").read_text()
    adr_0002 = (_DECISIONS / "0002-postgres-first-search.md").read_text()

    target = "0031-the-two-tier-suggest.md"
    assert target in prd_05, "PRD 05's autocomplete section does not link ADR-0031"
    assert target in adr_0002, "ADR-0002 does not point at the follow-up that discharges it"


def test_every_adr_titles_itself_with_its_own_number() -> None:
    """A renumber that moves the file and the citations can still leave the
    document introducing itself as the old number, and nothing else looks.

    **Found by hand on 2026-08-21, which is the argument for the case.** The
    bounded-column record moved `0041` -> `0043`; its filename moved, its
    register row moved, all 76 citations moved, and its own `# 0041 — ...`
    heading did not. Every other check in this file passes against that state:
    the register compares *filenames* to *links*, and the duplicate-number case
    reads the filename prefix. The H1 is the one place the number appears that
    nothing derived it from -- so a reader who opens the file is told the wrong
    number by the document itself, which is worse than an unlinked record.

    Scoped to the **number** rather than to the whole `# ADR-NNNN —` form on
    purpose. `0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md`
    titles itself `# 0030 — ...` with no `ADR-` prefix, which is a formatting
    drift that misleads nobody, and widening this case to catch it would mean
    editing a record on the trunk for style inside a milestone branch. The
    defect this exists for is a *wrong* number, not a missing prefix.
    """
    paths = sorted(_DECISIONS.glob("0*.md"))
    assert len(paths) >= 35, f"the register scan found only {len(paths)} ADRs"

    wrong = [
        f"{path.name} is titled {heading!r}"
        for path in paths
        if (heading := path.read_text().splitlines()[0]) and path.name[:4] not in heading
    ]
    assert wrong == [], "these ADRs do not name their own number in their heading: " + "; ".join(
        wrong
    )


# -- ADR-0046's numbers, and the three of them that need no database ---------


_ADR_0046 = _DECISIONS / "0046-the-scheduler-stores-nothing.md"


def _prose(path: pathlib.Path) -> str:
    """A document with its Markdown emphasis, its code ticks and its line
    breaks taken out.

    Anchoring on `**12,884 s**` pins where a sentence happened to wrap and
    which words were bold or ticked on the day, none of which is the fact.
    Stripping all three means a reflow is not a red and a *changed number* is.
    """
    return " ".join(path.read_text().replace("*", "").replace("`", "").split())


def _matched(pattern: str, text: str, what: str) -> re.Match[str]:
    """`re.search` with the premise asserted, because `None` here is the
    "a plant that did not land looks exactly like a check that passed" shape:
    a regex that stops matching after a rewrite would otherwise skip the
    arithmetic silently and report green."""
    match = re.search(pattern, text)
    assert match is not None, f"nothing in this document still states {what}"
    return match


def test_adr_0046s_arithmetic_over_its_own_stated_inputs_holds() -> None:
    """Three of ADR-0046's figures are **derived from other figures in the
    same sentence**, and those are the ones a test can own.

    This record is measured against a database that is not in this repository,
    so almost every number in it is dated prose by the convention `13d7b58`
    established: nothing here can re-run `SELECT count(*)` against a catalog a
    checkout does not carry, and a test that hard-coded the answer would be a
    second copy of the measurement rather than a check on it. **But an
    identity between numbers the document itself states needs no database at
    all**, and a review on 2026-08-27 falsified ten of this record's figures
    one at a time and watched all five gate steps stay green. These three are
    the ones that did not have to survive that:

    - `877 = 133,319 - 132,442` — the seeds with no neighbour row;
    - `12,884 s = 3.58 hours`, and the 12,884 s is itself the difference
      between two timestamps the same sentence prints;
    - `97.3 ms/seed = 12,884 s / 132,442 seeds`.

    A fourth candidate, *"a tick is ~144 ms (71.1 + 72.6)"*, is deliberately
    **not** pinned: the same review found the addition itself was the defect —
    `count_stale()` is the job's own guard inside `run()` and not a read the
    scheduler's contract performs — so the claim was corrected away rather
    than fastened down. Pinning arithmetic does not make the operands the
    right ones.
    """
    prose = _prose(_ADR_0046)

    population = _matched(
        r"([\d,]+) rows over ([\d,]+) seeds against ([\d,]+) embedded titles "
        r"— ([\d,]+) embedded titles have no neighbour row",
        prose,
        "the incomplete-artefact count as `<rows> rows over <seeds> seeds "
        "against <embedded> embedded titles — <missing> embedded titles have "
        "no neighbour row`",
    )
    seeds = int(population.group(2).replace(",", ""))
    embedded = int(population.group(3).replace(",", ""))
    missing = int(population.group(4).replace(",", ""))
    assert embedded - seeds == missing, (
        f"{embedded:,} embedded titles less {seeds:,} seeds carrying rows is "
        f"{embedded - seeds:,}, and this record says {missing:,}"
    )

    walk = _matched(
        r"min\(computed_at\) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)Z → "
        r"max\(computed_at\) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)Z "
        r"= ([\d,]+) s = ([\d.]+) hours over ([\d,]+) seeds, ([\d.]+) ms/seed",
        prose,
        "the completed walk as `min(computed_at) <t> → max(computed_at) <t> "
        "= <s> s = <h> hours over <seeds> seeds, <ms> ms/seed`",
    )
    started = datetime.strptime(walk.group(1), "%Y-%m-%d %H:%M:%S")
    finished = datetime.strptime(walk.group(2), "%Y-%m-%d %H:%M:%S")
    span_seconds = int(walk.group(3).replace(",", ""))
    hours = float(walk.group(4))
    walk_seeds = int(walk.group(5).replace(",", ""))
    per_seed_ms = float(walk.group(6))

    assert (finished - started).total_seconds() == span_seconds, (
        f"{walk.group(1)}Z to {walk.group(2)}Z is "
        f"{(finished - started).total_seconds():,.0f} s, and this record says "
        f"{span_seconds:,} s"
    )
    assert round(span_seconds / 3600, 2) == hours, (
        f"{span_seconds:,} s is {span_seconds / 3600:.2f} hours, and this record says {hours}"
    )
    assert round(span_seconds / walk_seeds * 1000, 1) == per_seed_ms, (
        f"{span_seconds:,} s over {walk_seeds:,} seeds is "
        f"{span_seconds / walk_seeds * 1000:.1f} ms/seed, and this record says "
        f"{per_seed_ms}"
    )


def test_the_walk_reads_the_same_length_in_every_document_that_prices_it() -> None:
    """One walk, four documents, and until this case nothing tied them.

    ADR-0046 measures the rebuild's own duration; PRD 08's `### Scheduled
    work` prices `USHER_SCHEDULER_ENABLED`'s default against it; PRD 09's M10
    row prices the same default against it again; and
    `usher.ports.scheduler.ScheduledJob.last_done` states the obligation it
    puts on a registration's `period`. **A figure restated in four places is
    the shape this repository keeps getting wrong** — the 148/136 percentages
    one subsystem over, `PortRateLimited`'s raise-site census, the twelve
    landings in `test_migrations.py` — and the failure is always the same: one
    site is re-measured and the others are not, so a reader's answer depends on
    which document they opened.

    The port's spelling is `3 h 34 m 44 s` rather than `3.58 h`, so this also
    checks the two forms are the same span. That is deliberate: the sweep that
    hunts `3.58` does not find `34 m 44 s`, which is exactly how the old
    `3.33 h / 91.7 ms/seed` figure survived in four places under a sweep that
    hunted the hours.
    """
    adr = _matched(
        r"= ([\d,]+) s = ([\d.]+) hours over ([\d,]+) seeds",
        _prose(_ADR_0046),
        "ADR-0046's completed walk",
    )
    span_seconds = int(adr.group(1).replace(",", ""))
    hours = float(adr.group(2))
    seeds = int(adr.group(3).replace(",", ""))

    operations = _matched(
        r"most recent completed rebuild took ([\d.]+) hours over ([\d,]+) seeds",
        _prose(_DECISIONS.parent / "08-operations.md"),
        "PRD 08's `### Scheduled work` price for the default",
    )
    assert (float(operations.group(1)), int(operations.group(2).replace(",", ""))) == (
        hours,
        seeds,
    ), f"PRD 08 prices the walk at {operations.group(1)} h over {operations.group(2)} seeds"

    roadmap = _matched(
        r"the job it would start is ([\d.]+) hours",
        _prose(_DECISIONS.parent / "09-roadmap.md"),
        "PRD 09's M10 row pricing the scheduler's default",
    )
    assert float(roadmap.group(1)) == hours, (
        f"PRD 09's M10 row prices the walk at {roadmap.group(1)} hours"
    )

    port = _prose(pathlib.Path(__file__).parents[2] / "src" / "usher" / "ports" / "scheduler.py")
    spelled_out = _matched(
        r"(\d+) h (\d+) m (\d+) s", port, "`ScheduledJob.last_done`'s walk duration"
    )
    stated = (
        int(spelled_out.group(1)) * 3600
        + int(spelled_out.group(2)) * 60
        + int(spelled_out.group(3))
    )
    assert stated == span_seconds, (
        f"the port spells the walk {spelled_out.group(0)} = {stated:,} s, and "
        f"ADR-0046 measured {span_seconds:,} s"
    )
    decimal = _matched(
        r"at or under ([\d.]+) h", port, "`ScheduledJob.last_done`'s back-to-back bound"
    )
    assert float(decimal.group(1)) == hours, (
        f"the port bounds a period at {decimal.group(1)} h against ADR-0046's {hours}"
    )
