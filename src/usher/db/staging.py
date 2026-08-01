"""`COPY` into an `UNLOGGED` staging table -- the one path every bulk write
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
    """Create a per-batch `UNLOGGED` staging table and `COPY` into it.

    `UNLOGGED` skips WAL entirely -- the data is re-derivable and a crash
    mid-batch rolls the batch back anyway. `DROP ... IF EXISTS` first rather
    than reusing the table across batches: the caller commits between
    batches, so a leftover table from a crashed batch would otherwise merge
    into the next one.

    `table` and `ddl` are interpolated into SQL, so neither may ever come
    from anything a caller does not control -- every call site in this
    project passes a module-level constant.
    """
    await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    await session.execute(text(ddl))
    driver = await raw_connection(session)
    await driver.copy_records_to_table(table, records=records, columns=list(columns))
