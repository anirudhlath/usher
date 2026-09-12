import re
from pathlib import Path

from alembic.script import ScriptDirectory

import usher.db.migrations as _migrations_package
from usher.db.migrations.status import code_head_revision


def test_code_head_revision_matches_the_head_migration_on_disk() -> None:
    """No Docker needed: reads usher/db/migrations/versions/*.py directly
    off disk, the same files `alembic upgrade head` itself would use --
    doesn't touch a database at all. Pinned to the literal revision id (not
    just "is not None") so a migration ever added without updating this test
    fails loudly here instead of silently changing what "the" expected head
    means.
    """
    assert code_head_revision() == "m10d"


#: The chain three documents spell out, in the order they spell it: every
#: revision from `ffa` -- the landing that created
#: `test_migrations.py`'s `-1` block -- to head inclusive. A literal for
#: `test_code_head_revision_matches_the_head_migration_on_disk`'s reason, so
#: the fourteenth landing is a loud red here rather than a silent
#: disagreement with the prose.
_REPOINTING_CHAIN = (
    "ffa",
    "ffb",
    "ffc",
    "m08a",
    "m08b",
    "m09a",
    "m09c",  # `m09b` was never minted; the gap is deliberate and is not a landing.
    "m09d",
    "m09e",
    "m09f",
    "m10a",
    "m10b",
    "m10c",
    "m10d",
)

#: The English cardinal `.claude/rules/db-and-sql.md` and
#: `tests/integration/test_migrations.py` both write out. Keyed by count so
#: the next landing changes one literal and the word follows it.
_CARDINALS = {12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen"}

#: The two documents that state the count in prose. Neither is checked by
#: anything else: `db-and-sql.md` is a rules file and the other is a
#: docstring, so both are exactly the shape that went **five landings stale**
#: between `m09a` and `m10b` -- which `db-and-sql.md` records, in the entry
#: whose own subject is a count going stale.
_COUNT_SITES = (
    Path(".claude/rules/db-and-sql.md"),
    Path("tests/integration/test_migrations.py"),
)


def _chain_to_head() -> tuple[str, ...]:
    """`ffa` to head inclusive, walked rather than listed.

    `ScriptDirectory` over the package's own `versions/` directory, which is
    `code_head_revision`'s spelling and works under an editable install and
    a wheel alike -- never `Config("alembic.ini")`, whose relative path is
    correct only when the process happens to be rooted at the repository.
    """
    (location,) = _migrations_package.__path__
    scripts = ScriptDirectory(location)
    head = scripts.get_current_head()
    assert head is not None, "the premise: exactly one head, so there is a chain to walk"
    walked = [script.revision for script in scripts.walk_revisions(base="base", head=head)]
    assert "ffa" in walked, "the premise: the walk reached `ffa`, so the slice below is real"
    return tuple(reversed(walked[: walked.index("ffa") + 1]))


def test_the_repointing_chain_on_disk_is_the_one_three_documents_spell_out() -> None:
    """**A counted fact restated in three places goes stale in the two nobody
    re-reads**, and this repository has the receipt: `.claude/rules/db-and-sql.md`
    records the count standing at *six* from `m09a` (2026-08-10) until issue
    #41 brought it current on 2026-08-25 -- five landings that each re-pointed
    `test_migrations.py`'s `-1` block and none of which wrote it down.

    So the chain is compared against a **scan** rather than restated a fourth
    time. `ffa` is the lower bound because that is the landing that created
    the block; every revision above it broke the block and re-pointed it, so
    the count of landings and the length of this chain are one number, and a
    revision that lands without re-pointing fails
    `test_a_full_down_and_up_cycle_restores_every_index` rather than passing
    here quietly.

    Both premises are asserted inside the walk, for the reason `m10c`'s own
    red carried: an empty versions directory makes `get_current_head()`
    answer `None` and a slice of an empty list is `()`, which would satisfy
    an equality against nothing at all.
    """
    assert _chain_to_head() == _REPOINTING_CHAIN


def test_the_landing_count_the_prose_states_is_the_one_on_disk() -> None:
    """The half a chain comparison cannot see: two documents write the count
    out **in words**, and a landing that re-points the block without touching
    them leaves the repository asserting one number and explaining another.

    Only the phrase *"<cardinal> landings"* is matched, not every occurrence
    of the word -- `thirteen` appears in over thirty unrelated places in this
    tree (`RowContext`'s fields, the candidate cap, a TMDb run's non-200s),
    so a bare substring search would be a change-detector on prose that has
    nothing to do with migrations.
    """
    expected = _CARDINALS[len(_REPOINTING_CHAIN)]
    stale = {word for count, word in _CARDINALS.items() if count != len(_REPOINTING_CHAIN)}
    for site in _COUNT_SITES:
        text = site.read_text(encoding="utf-8")
        # The premise: the file was found and really does state a count.
        # Read relative to the rootdir pytest is invoked from, which is why a
        # missing file has to be a failure rather than a skipped site.
        found = {match.lower() for match in re.findall(r"(\w+) landings\b", text, re.IGNORECASE)}
        assert found, f"{site} no longer states a landing count at all"
        assert expected in found, (
            f"{site} states {sorted(found)} landings, and the chain has {expected}"
        )
        assert not (found & stale), (
            f"{site} still states a superseded count: {sorted(found & stale)}"
        )
