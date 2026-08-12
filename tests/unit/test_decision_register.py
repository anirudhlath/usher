"""The ADR register is hand-maintained and nothing checked it until this
file existed.

Same argument M6 made for `ALL_PORTS`: twenty-two rows kept correct by
attention rather than by construction. An ADR that is written and not
listed is a document nobody finds, and the failure is silent in both
directions -- a missing row, or a row pointing at a file that was renamed.
"""

import pathlib
import re

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
