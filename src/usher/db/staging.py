"""`COPY` into a **temporary** staging table -- the one path every bulk write
in this project takes.

Extracted verbatim from `usher.db.repositories.bulk`'s `_stage`/`_raw`,
which is where the three Postgres facts this is built around were measured
(2026-07-30, `pgvector/pgvector:pg17`) and where they are documented in
full. Restated here only as far as a caller needs:

1. `ON CONFLICT` must repeat a partial index's predicate.
2. One statement may not hit the same conflict target twice, so every
   staging read is `SELECT DISTINCT ON (<target>)`. **For `media_items` this
   is required rather than defensive:** `SourceAdapter.list_items`' own
   contract says "the same item may be yielded more than once in a single
   walk", so a batch genuinely does contain duplicates.
3. `xmax = 0` in `RETURNING` is the only way to tell an insert from an
   update.

asyncpg's binary `COPY` is strictly typed -- a `str` into an `integer`
column raises `TypeError` client-side before a byte reaches Postgres -- and
CHECK constraints fire during `COPY`, so one bad row aborts its batch.
Conversion belongs in whatever built the records, not here.

**The staging tables this creates deliberately carry no constraints**, so
in practice a value that violates the *destination's* CHECK survives the
`COPY` and fails one statement later, at the `INSERT ... SELECT`. That
statement goes through SQLAlchemy, so it surfaces as
`sqlalchemy.exc.IntegrityError` and a repository can translate it;
`copy_records_to_table` is called on the raw asyncpg connection and would
raise `asyncpg.exceptions.CheckViolationError` straight through SQLAlchemy's
error translation instead. Verified directly against
`pgvector/pgvector:pg17` (2026-07-31) with a `width = -1` media item: the
`COPY` succeeds, the upsert raises. Do not add constraints to a staging DDL
without giving its caller a second `except` clause.

**Every `ddl` passed here must be `CREATE TEMP TABLE ... ON COMMIT DROP`,
and that is a correctness precondition rather than a style rule.** The
staging names are module constants shared by every caller in the
deployment, and until M6 they named tables in `public`. Three consequences,
all measured 2026-08-03 against `pgvector/pgvector:pg17` and all reproduced
through the shipped `PostgresJobQueue` before the change:

1. **With a leftover table, two concurrent callers serialise for the length
   of the other's whole transaction.** `DROP TABLE` and `CREATE TABLE` each
   take `ACCESS EXCLUSIVE` and both are held to commit, so the wait is not
   the length of a DDL. Measured at 819 ms against an 800 ms hold, in
   lockstep.
2. **With *no* leftover table the failure is not a wait at all.** Two
   backends creating the same public name at once race on
   `pg_type_typname_nsp_index`; asyncpg raises `UniqueViolationError`,
   SQLAlchemy wraps it as `IntegrityError`, and `PostgresJobQueue`'s own
   handler translates it to `RepositoryConflict`. A perfectly healthy batch
   is reported to its caller as a constraint violation, and the only thing
   wrong with it was the instant it ran.
3. **A caller that commits leaves the table behind.** Postgres DDL is
   transactional, so this is invisible under the integration suite's usual
   rolled-back isolation and surfaces as schema drift in
   `test_migration_matches_the_orm_metadata` -- in a *later file*, so the
   suite that caused it passes alone. Nine integration files carried an
   explicit `DROP TABLE IF EXISTS stg_*` line for exactly this.

A temporary table removes all three: the relation lives in the session's own
`pg_temp` schema, so there is nothing shared to lock and nothing shared to
collide with in `pg_type`, and `ON COMMIT DROP` removes it at the commit
that would otherwise have persisted it -- which is also what makes it
pool-safe, since a temporary table outliving its transaction would ride a
pooled connection into whatever checked it out next.
`inspect(conn).get_table_names()` does not see it either, which is what
deletes the drift.

**`CREATE TEMP UNLOGGED TABLE` is a syntax error**, verified: `TEMP`
replaces `UNLOGGED` rather than joining it, and a temporary table is already
WAL-free. `tests/unit/test_staging_ddl.py` scans `src/` for both halves,
because the precondition lives on a string constant in five different
modules and the eleventh one added is the one that forgets.

**The measured cost, and why there is no second small-batch path.** A
one-row staged upsert through a temporary table costs 4.46 ms p50 against
3.14 ms for the same upsert over an inline `VALUES` list (1.42x, +1.32 ms),
plus about 2.9 dead `pg_class` tuples per call. That is a cost, not a
correctness property, and it is paid on a path that already makes a network
round trip per item. Against it, a threshold-selected second spelling of
`merge_from_source`'s four statements would be a second place ADR-0014's
`NULL` `play_count` can collapse to `0` -- silent, permanent, and measured
in that module at 7 -> 0 -- and it would fix nothing at all for
`media_items`, `seasons`, `episodes` or `title_embeddings`, whose callers no
row threshold routes away from staging.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def raw_connection(session: AsyncSession) -> Any:
    """The live `asyncpg.Connection` under this session.

    `AsyncSession.connection()` gives SQLAlchemy's `AsyncConnection`;
    `get_raw_connection().driver_connection` unwraps two more layers to the
    real driver object (verified: `asyncpg.connection.Connection`, carrying
    `copy_records_to_table`). Typed `Any` because asyncpg ships no stubs and
    SQLAlchemy types `driver_connection` as `Any` itself, so a narrower
    annotation would be a fiction mypy could not check.

    Runs `session.connection()` under `no_autoflush` for the same reason
    every read in `PostgresTitleRepository` does: it flushes by default, and
    a shared session may be carrying someone else's pending, invalid state.
    """
    with session.no_autoflush:
        connection = await session.connection()
    return (await connection.get_raw_connection()).driver_connection


async def stage_records(
    session: AsyncSession,
    *,
    ddl: str,
    table: str,
    columns: Sequence[str],
    records: Sequence[tuple[Any, ...]],
) -> None:
    """Create a per-batch **temporary** staging table and `COPY` into it.

    A temporary table skips WAL entirely -- the data is re-derivable and a
    crash mid-batch rolls the batch back anyway -- and it is private to this
    session, which is the whole of the module docstring's argument above.
    `DROP ... IF EXISTS` first rather than reusing the table: `ON COMMIT
    DROP` handles the commit boundary, but a caller may stage twice *before*
    one (`IngestService` enqueues match jobs and then watch-history jobs on
    the same session), and the second `CREATE TEMP TABLE` would otherwise
    meet the first still standing.

    **The drop is `pg_temp`-qualified and that is load-bearing.** Unqualified,
    it resolves through `search_path`, and on the first call of a session
    that reaches a *leftover* `public.stg_jobs` -- from a release predating
    this change, or a crash under one -- it takes `ACCESS EXCLUSIVE` on the
    shared name and reintroduces the 819 ms stall exactly once per
    deployment, which is the shape of bug nobody can reproduce. Qualified, a
    leftover is inert: the `CREATE TEMP TABLE` that follows puts a temporary
    relation in front of it in `search_path`, so the `COPY` and the caller's
    `INSERT ... SELECT` both resolve to the temporary one. Verified against a
    deliberate `public`/`pg_temp` shadow -- the `COPY` landed 1 row in
    `pg_temp` and 0 in `public`. Migration `fc6d2b81a794` drops the leftovers
    anyway; this is what makes that migration cleanup rather than a
    prerequisite.

    `table` and `ddl` are interpolated into SQL, so neither may ever come
    from anything a caller does not control -- every call site in this
    project passes a module-level constant.
    """
    await session.execute(text(f"DROP TABLE IF EXISTS pg_temp.{table}"))
    await session.execute(text(ddl))
    driver = await raw_connection(session)
    # Unqualified, deliberately: asyncpg builds `COPY "stg_jobs" (...)` and
    # Postgres resolves it through `search_path`, which puts `pg_temp` first.
    # Naming the schema would mean spelling the session's own temp namespace,
    # which is a per-backend name (`pg_temp_3`) this has no way to know.
    await driver.copy_records_to_table(table, records=records, columns=list(columns))
