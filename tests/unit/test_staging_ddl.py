"""Every staging table in `src/` is temporary, and it drops at commit.

A rule rather than a list, because the list is what drifts. Ten staging DDLs
exist across five repositories today and the eleventh is the one that
reintroduces the hazard: a shared name in `public` taking two `ACCESS
EXCLUSIVE` locks per batch, held to commit, plus a table left behind by any
caller that commits -- which surfaces as schema drift in
`test_migration_matches_the_orm_metadata`, in a *later file*, so the suite
that caused it passes alone.

Scanned as text rather than asserted per module, for the reason
`test_every_setting_is_read_by_something` is a substring scan: the DDL is a
string constant in five different files and a per-file assertion is a list
again. It cannot tell a DDL from a docstring quoting one, which is why the
match requires `CREATE` and a `stg_` name on the same line -- prose about the
old shape reads `DROP TABLE IF EXISTS stg_jobs` or names the constant, never
`CREATE ... TABLE stg_x (`.
"""

import re
from pathlib import Path

from usher.db.migrations.versions.fc6d2b81a794_drop_leftover_staging_tables import (
    _LEFTOVER_STAGING_TABLES,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "usher"

# `CREATE [UNLOGGED|TEMP|TEMPORARY] TABLE stg_<name>` in any spelling, with the
# modifier captured so a bare `CREATE TABLE` is a match rather than a miss --
# the failure this guards against is precisely a DDL that forgot the modifier.
_CREATE = re.compile(r"CREATE\s+(?P<modifier>[A-Z]*\s*)TABLE\s+(?P<name>stg_\w+)", re.IGNORECASE)


def _close_paren(text: str, opened_at: int) -> int:
    """The index of the paren matching the one at `opened_at`.

    Depth-counted rather than `text.index(")")`, because a column list holds
    `varchar(16)` and the first close paren is not the statement's --
    a scan that stopped there would read `) ON COMMIT DROP` as absent on
    exactly the DDLs that have a sized type in them.
    """
    depth = 0
    for index in range(opened_at, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError(f"unbalanced parentheses from offset {opened_at}")


def _staging_ddls() -> list[tuple[Path, str, str]]:
    """Every `CREATE ... TABLE stg_*` in `src/`, whitespace-normalised."""
    found: list[tuple[Path, str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text()
        for match in _CREATE.finditer(text):
            close = _close_paren(text, text.index("(", match.end()))
            # Everything up to the end of the statement, which for a Python
            # string constant is the closing quote or the next statement.
            tail = text[match.start() : close + 1] + text[close + 1 : close + 32].split('"""')[0]
            found.append((path, match.group("name"), " ".join(tail.split())))
    return found


def test_the_scan_finds_the_staging_ddl_it_is_scanning_for() -> None:
    """A guard that globs nothing passes exactly like a guard that passes.

    The same control `test_no_dataset_row_is_committed_anywhere` carries, and
    for the same reason: this file's only claim is about what it found, so it
    has to have found something. Six repositories stage today -- jobs,
    watch_states, media_items, seasons/episodes, title_embeddings and the four
    bulk tables.
    """
    ddls = _staging_ddls()
    assert len(ddls) >= 10, f"the scan found only {len(ddls)} staging DDLs: {ddls}"
    assert {"stg_jobs", "stg_watch_states", "stg_title_embeddings"} <= {name for _, name, _ in ddls}


def test_every_staging_table_is_temporary_and_drops_at_commit() -> None:
    """The wrong implementation: `CREATE UNLOGGED TABLE stg_jobs`, which is
    what every one of these was until M6.

    `TEMP` is what removes the contention *and* the `pg_type` race: a
    temporary table lives in the session's own `pg_temp` schema, so two
    backends creating one at the same instant neither wait for each other nor
    collide on `pg_type_typname_nsp_index` -- which asyncpg reports as
    `UniqueViolationError` and SQLAlchemy wraps as `IntegrityError`,
    indistinguishable to a repository from a genuine data conflict.

    `ON COMMIT DROP` is what makes it pool-safe and deletes the cleanup nine
    integration files used to carry: without it a temporary table outlives the
    transaction and rides a pooled connection into whatever checks it out
    next, which is a *worse* leak than the public one because nothing outside
    that session can see it to drop it.

    **`CREATE TEMP UNLOGGED TABLE` is a syntax error**, verified against
    `pgvector/pgvector:pg17`: `TEMP` replaces `UNLOGGED` rather than joining
    it, and a temporary table is already WAL-free. So the modifier is exactly
    `TEMP`, never both.
    """
    wrong = [
        (path.name, name, statement)
        for path, name, statement in _staging_ddls()
        if not re.search(r"CREATE\s+TEMP\s+TABLE", statement, re.IGNORECASE)
        or "ON COMMIT DROP" not in statement.upper()
    ]
    assert not wrong, (
        f"every staging DDL must be `CREATE TEMP TABLE ... ON COMMIT DROP`; these are not: {wrong}"
    )


def test_the_leftover_migration_names_every_staging_table() -> None:
    """Migration `fc6d2b81a794` drops the `public.stg_*` tables a release
    predating the temporary ones may have left behind, and it enumerates them
    rather than globbing `pg_class` -- a wildcard over someone else's schema
    is a migration that destroys data it was never told about.

    So the list has to stay complete, and nothing else makes it. **This does
    not kill "delete the loop body"**, and that is deliberate rather than a
    gap: `stage_records` drops `pg_temp.<table>` explicitly, so a leftover is
    inert whether or not the migration ran, and the migration is cleanup. What
    it kills is the eleventh staging table, added with its own DDL and no line
    here -- which would leave a permanent `public.stg_<new>` on every
    deployment that ever ran the old code.
    """
    missing = {name for _, name, _ in _staging_ddls()} - set(_LEFTOVER_STAGING_TABLES)
    assert not missing, (
        f"{sorted(missing)} stage in src/ but migration fc6d2b81a794 does not drop "
        "the public leftover a pre-M6 release would have made"
    )
