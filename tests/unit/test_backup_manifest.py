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
"""

import argparse
from collections import Counter

import pytest

from usher import cli as usher_cli
from usher.db import models  # noqa: F401  -- registers every table on Base.metadata
from usher.db.backup_manifest import ALEMBIC_VERSION_TABLE, MANIFEST, BackupClass, RestoreRule
from usher.db.base import Base


def _subcommands() -> set[str]:
    """`build_parser()`'s own subcommand list, never a hand-copied one.

    The count has moved twice since this manifest was designed (15 -> 17),
    so a literal here would be a second list to forget, which is the exact
    failure this task exists to fix one directory over.
    """
    subparsers = next(
        action
        for action in usher_cli.build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices)


def test_every_entry_carries_a_class_from_the_enum() -> None:
    for table, entry in MANIFEST.items():
        assert isinstance(entry.kind, BackupClass), table


def test_the_class_counts_are_the_ones_this_task_argued_for() -> None:
    """Asserted as counts, not merely as coverage, so moving a table between
    columns is a diff a reviewer sees rather than a silent reclassification
    of the household's watch state as something an importer can rebuild.
    """
    assert Counter(entry.kind for entry in MANIFEST.values()) == {
        BackupClass.REBUILDABLE: 20,
        BackupClass.PRECIOUS: 7,
        BackupClass.PARTIAL: 1,
        BackupClass.SCHEMA: 1,
    }
    assert len(MANIFEST) == 29


def test_the_manifest_is_the_orm_metadata_plus_alembics_own_table() -> None:
    """The set equality, spelled against `Base.metadata` so it needs no
    Docker. `alembic_version` has no `Table` object anywhere in `src/` --
    Alembic creates it -- so it is named on the right-hand side rather than
    special-cased away.
    """
    assert set(MANIFEST) == set(Base.metadata.tables) | {ALEMBIC_VERSION_TABLE}


def test_every_entry_carries_a_reason() -> None:
    for table, entry in MANIFEST.items():
        assert entry.reason.strip(), table


def test_every_partial_entry_names_columns_that_exist_on_the_model() -> None:
    """Read off `Base.metadata`, so renaming `media_items.title_id` is a red
    here rather than a manifest that silently protects a column that no
    longer exists.
    """
    partial = {
        table: entry for table, entry in MANIFEST.items() if entry.kind is BackupClass.PARTIAL
    }
    assert partial, "the positive control: a PARTIAL entry has to exist for this to mean anything"
    for table, entry in partial.items():
        assert entry.columns, table
        existing = set(Base.metadata.tables[table].columns.keys())
        assert set(entry.columns) <= existing, f"{table}: {sorted(set(entry.columns) - existing)}"


def test_every_rebuildable_entry_carries_a_rebuild_command() -> None:
    """A rebuildable table is a claim about a command an operator can run.
    An entry that makes the claim without naming the command is the prose
    the manifest replaces.
    """
    for table, entry in MANIFEST.items():
        if entry.kind is BackupClass.REBUILDABLE:
            assert entry.rebuilt_by.strip(), table


def test_every_rebuildable_entry_names_a_command_the_cli_advertises() -> None:
    """Every step, not only the first, so a plausible-looking second clause
    (`... then restore --catalog`) cannot hide behind a real first one.
    CLAUDE.md's standing rule: do not invent commands for tooling that does
    not exist yet.
    """
    advertised = _subcommands()
    assert "bootstrap" in advertised, "the positive control: the parser was read, not an empty set"
    for table, entry in MANIFEST.items():
        for command in entry.rebuild_commands:
            assert command.split()[0] in advertised, f"{table}: {command!r}"


def test_the_optional_fields_belong_to_the_class_that_defines_them() -> None:
    """`columns` is PARTIAL's and `rebuilt_by` is REBUILDABLE's. A precious
    table carrying a rebuild command would be an argument for moving it, made
    in a field nothing reads.
    """
    for table, entry in MANIFEST.items():
        if entry.kind is not BackupClass.PARTIAL:
            assert entry.columns == (), table
        if entry.kind is not BackupClass.REBUILDABLE:
            assert entry.rebuilt_by == "", table


@pytest.mark.parametrize(
    ("kind", "rule"),
    [
        (BackupClass.PRECIOUS, RestoreRule.WHOLE),
        (BackupClass.PARTIAL, RestoreRule.MERGE),
        (BackupClass.REBUILDABLE, RestoreRule.NEVER),
        (BackupClass.SCHEMA, RestoreRule.NEVER),
    ],
)
def test_the_restore_rule_follows_the_class(kind: BackupClass, rule: RestoreRule) -> None:
    """The rule is stored per entry rather than derived from the class at
    read time, because K4 reads one field and not a lookup table -- but it
    is not free to disagree with the class, and this is what says so.
    """
    entries = [entry for entry in MANIFEST.values() if entry.kind is kind]
    assert entries, kind
    assert {entry.restore for entry in entries} == {rule}


def test_import_runs_is_never_restored_and_the_manifest_says_so_rather_than_k4() -> None:
    """`import_runs` is the entry where `NEVER` is load-bearing rather than
    incidental. For every other rebuildable table "never written" follows
    from "never carried"; these rows would be harmful *even if* an operator
    carried them by hand, because they tell a resumable importer a phase is
    complete over a catalog that is empty.
    """
    assert MANIFEST["import_runs"].restore is RestoreRule.NEVER
