import re
from pathlib import Path

from alembic.script import ScriptDirectory

import usher.db.migrations as _migrations_package
from usher.db.migrations.status import code_head_revision


def test_code_head_revision_matches_the_head_migration_on_disk() -> None:
    """The head on disk, read without Docker.

    Reads `usher/db/migrations/versions/*.py` directly, the same files `alembic
    upgrade head` would use. Pinned to the literal revision id, not just "is not
    None", so a migration added without updating this test fails loudly instead
    of silently changing what "the" expected head means.
    """
    assert code_head_revision() == "m10f"


#: Every revision from `ffa` -- the landing that created `test_migrations.py`'s
#: `-1` block -- to head inclusive.
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
    "m10e",
    "m10f",
)

#: The English cardinal `.claude/rules/db-and-sql.md` writes out. Keyed by
#: count so the next landing changes one literal and the word follows it.
_CARDINALS = {
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
}

#: The document that states the count in prose. A rules file is read by people
#: and by nothing else, so it is the shape that goes stale unnoticed.
_COUNT_SITES = (Path(".claude/rules/db-and-sql.md"),)


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
    """The chain is compared against a scan of `versions/` rather than restated.

    `ffa` is the lower bound because that is the landing that created
    `test_migrations.py`'s `-1` block; every revision above it re-pointed the
    block, so the count of landings and the length of this chain are one number.
    Both premises are asserted inside the walk: an empty versions directory makes
    `get_current_head()` answer `None`, and a slice of an empty list is `()`,
    which would satisfy an equality against nothing at all.
    """
    assert _chain_to_head() == _REPOINTING_CHAIN


def test_the_landing_count_the_prose_states_is_the_one_on_disk() -> None:
    """The half a chain comparison cannot see: the count written out in words.

    A landing that re-points the block without editing the rules file leaves the
    repository asserting one number and explaining another. Only the phrase
    *"<cardinal> landings"* is matched, not every occurrence of the word, which
    appears in many places that have nothing to do with migrations.
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
