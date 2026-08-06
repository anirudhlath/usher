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
    """
    register = (_DECISIONS / "README.md").read_text()
    files = {path.name for path in _DECISIONS.glob("0*.md")}

    assert len(files) >= 23, f"the register scan found only {len(files)} ADRs"
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
