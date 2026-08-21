"""The invariants of `usher.db.backup_manifest` that need no container.

The integration arm
(`tests/integration/test_backup_manifest_covers_the_live_schema.py`) reads
`information_schema` and is the only thing that can see `alembic_version`.
Everything here is a fact about the mapping itself or about
`Base.metadata`, so it runs on the fast path -- which is what makes an M11
model that adds a table a red before anyone starts Docker.

**The two arms overlap on coverage and that overlap is deliberate**, but
they are not the same assertion: the integration arm compares the manifest
to a *database*, this one compares it to the *ORM metadata*. A migration
that creates a table no model declares fails only the first; a model added
without a migration fails only the second.

⚠️ **Three of this file's cases were measured green with the defect
planted and are rewritten because of it.** The set-equality named
`ALEMBIC_VERSION_TABLE` on both sides, so the constant cancelled and could
be set to anything; the CLI check had a positive control on the parser and
none on its own subject, so an empty `rebuild_commands` skipped the loop
for all 29 entries; and the `PARTIAL` columns were asserted as a *subset*,
so dropping `episode_id` -- halving what K4's merge carries -- passed.
The shape-level invariants they used to assert are now enforced in
`BackupEntry.__post_init__`, which holds for the entries K3 and future
fixtures construct and this file never sees; what remains here is
behavioural: that the constructor really does refuse, and that the mapping
really does say what this task argued.
"""

import shlex
from collections import Counter

import pytest

from usher.cli import build_parser
from usher.db import models  # noqa: F401  -- registers every table on Base.metadata
from usher.db.backup_manifest import (
    ALEMBIC_VERSION_TABLE,
    MANIFEST,
    BackupClass,
    BackupEntry,
    RestoreRule,
    tables_of,
)
from usher.db.base import Base


def test_every_entry_carries_a_class_from_the_enum() -> None:
    for table, entry in MANIFEST.items():
        assert isinstance(entry.kind, BackupClass), table


def test_the_class_counts_are_the_ones_this_task_argued_for() -> None:
    """Asserted as counts, not merely as coverage, so moving a table between
    columns is a diff a reviewer sees rather than a silent reclassification
    of the household's watch state as something an importer can rebuild.

    There is deliberately no `len(MANIFEST) == 29` beside this: the four
    counts pin the total by arithmetic, so such a line could not fail. The
    one thing it might have been read as covering -- a table name written
    twice in the literal, which a dict silently accepts -- is caught by
    `ruff`'s `F601` at gate step 1, verified against a probe file.
    """
    assert Counter(entry.kind for entry in MANIFEST.values()) == {
        BackupClass.REBUILDABLE: 20,
        BackupClass.PRECIOUS: 7,
        BackupClass.PARTIAL: 1,
        BackupClass.SCHEMA: 1,
    }


def test_the_manifest_is_the_orm_metadata_plus_alembics_own_table() -> None:
    """`"alembic_version"` is spelled out rather than written as the
    constant. Naming `ALEMBIC_VERSION_TABLE` on both sides of this equality
    -- the manifest keys it too -- made the constant cancel: it was measured
    green with the constant set to `"alembic_versionZZZ"`.
    """
    assert set(MANIFEST) == set(Base.metadata.tables) | {"alembic_version"}


def test_the_alembic_constant_names_the_table_alembic_really_creates() -> None:
    """The half the equality above cannot see. Alembic's default
    `version_table` is this string, and it is what the integration arm finds
    in `information_schema`.
    """
    assert ALEMBIC_VERSION_TABLE == "alembic_version"


def test_media_items_names_both_link_columns_and_they_exist_on_the_model() -> None:
    """The pair by name, not `<=` the model's columns. As a subset test this
    was measured green with `episode_id` deleted -- which would silently
    halve what K4's MERGE carries, and an episode link is exactly the
    operator judgement the match ladder is worst at.
    """
    entry = MANIFEST["media_items"]
    assert entry.kind is BackupClass.PARTIAL
    assert entry.columns == ("title_id", "episode_id")
    existing = set(Base.metadata.tables["media_items"].columns.keys())
    assert set(entry.columns) <= existing, sorted(set(entry.columns) - existing)


def test_every_rebuild_step_is_a_command_the_cli_really_accepts() -> None:
    """Parsed, not token-matched, and counted.

    Two defects were measured green against the token-matching version.
    Making `rebuild_commands` return `()` unconditionally skipped the loop
    body for all 29 entries -- hence `checked`. And matching only the first
    token accepted `bootstrap --phase there-is-no-such-phase` and
    `derive --restore-catalog --from-artifact`, because `--phase` is a closed
    `choices=` vocabulary and an unknown flag is an error argparse already
    knows how to raise. `parse_args` catches all three families at once and
    needs no reach into `argparse._SubParsersAction`.
    """
    parser = build_parser()
    checked = 0
    for table, entry in MANIFEST.items():
        if entry.kind is BackupClass.REBUILDABLE:
            assert entry.rebuild_commands, f"{table}: rebuilt_by parsed to no steps"
        for command in entry.rebuild_commands:
            try:
                parser.parse_args(shlex.split(command))
            except SystemExit:
                pytest.fail(f"{table}: `usher {command}` is not something the CLI accepts")
            checked += 1
    assert checked >= 20, f"only {checked} steps were parsed; every rebuildable entry owes one"


def test_import_runs_is_never_restored_and_the_manifest_says_so_rather_than_k4() -> None:
    """`import_runs` is the entry where `NEVER` is load-bearing rather than
    incidental. For every other rebuildable table "never written" follows
    from "never carried"; these rows would be harmful *even if* an operator
    carried them by hand, because they tell a resumable importer a phase is
    complete over a catalog that is empty.
    """
    assert MANIFEST["import_runs"].restore is RestoreRule.NEVER


def test_tables_of_answers_one_class_in_manifest_order() -> None:
    """Membership, then order — and the order is asserted as *"a subsequence
    of `MANIFEST`'s own keys"* rather than by naming which precious table
    comes first.

    Naming one would make the literal's ordering load-bearing, and it
    deliberately is not: the sweep's equivalent-mutant control swaps two
    `PRECIOUS` entries in the mapping and must stay green. The first
    spelling of this case asserted `precious[0] == "users"` and turned that
    control red, which is a test inventing a contract rather than pinning
    one.
    """
    assert tables_of(BackupClass.PARTIAL) == ("media_items",)
    assert tables_of(BackupClass.SCHEMA) == ("alembic_version",)

    precious = tables_of(BackupClass.PRECIOUS)
    assert set(precious) == {
        table for table, entry in MANIFEST.items() if entry.kind is BackupClass.PRECIOUS
    }
    keys = list(MANIFEST)
    positions = [keys.index(table) for table in precious]
    assert positions == sorted(positions), "tables_of reordered the manifest"
    # The premise that gives the line above teeth: manifest order is not
    # alphabetical, so a `sorted()` implementation could not satisfy it.
    assert precious != tuple(sorted(precious))


def test_a_valid_entry_constructs() -> None:
    """The positive control for the four refusals below: without it, "the
    constructor raises" is also what a constructor that raises on everything
    produces.
    """
    entry = BackupEntry(kind=BackupClass.PRECIOUS, restore=RestoreRule.WHOLE, reason="a reason")
    assert entry.rebuild_commands == ()


def test_an_entry_with_no_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="reason"):
        BackupEntry(kind=BackupClass.PRECIOUS, restore=RestoreRule.WHOLE, reason="   ")


def test_an_entry_whose_restore_rule_contradicts_its_class_is_refused() -> None:
    with pytest.raises(ValueError, match="restores as"):
        BackupEntry(kind=BackupClass.PRECIOUS, restore=RestoreRule.NEVER, reason="carried")


@pytest.mark.parametrize(
    ("kind", "restore", "columns"),
    [
        (BackupClass.PARTIAL, RestoreRule.MERGE, ()),
        (BackupClass.PRECIOUS, RestoreRule.WHOLE, ("title_id",)),
    ],
)
def test_columns_are_named_iff_the_entry_is_partial(
    kind: BackupClass, restore: RestoreRule, columns: tuple[str, ...]
) -> None:
    """Both directions. A `PARTIAL` entry naming nothing is a restore that
    merges nothing; a `PRECIOUS` entry naming columns is an argument for
    narrowing it, made in a field nothing reads.
    """
    with pytest.raises(ValueError, match="columns"):
        BackupEntry(kind=kind, restore=restore, reason="r", columns=columns)


@pytest.mark.parametrize(
    ("kind", "restore", "rebuilt_by"),
    [
        (BackupClass.REBUILDABLE, RestoreRule.NEVER, "  "),
        (BackupClass.PRECIOUS, RestoreRule.WHOLE, "sync"),
    ],
)
def test_a_rebuild_command_is_named_iff_the_entry_is_rebuildable(
    kind: BackupClass, restore: RestoreRule, rebuilt_by: str
) -> None:
    with pytest.raises(ValueError, match="rebuild command"):
        BackupEntry(kind=kind, restore=restore, reason="r", rebuilt_by=rebuilt_by)
