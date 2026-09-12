"""`COPY` into a **temporary** staging table -- the one path every bulk write in this
project takes.
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
    """Create a per-batch **temporary** staging table and `COPY` into it."""
    await session.execute(text(f"DROP TABLE IF EXISTS pg_temp.{table}"))
    await session.execute(text(ddl))
    driver = await raw_connection(session)
    # Unqualified, deliberately: asyncpg builds `COPY "stg_jobs" (...)` and
    # Postgres resolves it through `search_path`, which puts `pg_temp` first.
    # Naming the schema would mean spelling the session's own temp namespace,
    # which is a per-backend name (`pg_temp_3`) this has no way to know.
    await driver.copy_records_to_table(table, records=records, columns=list(columns))
