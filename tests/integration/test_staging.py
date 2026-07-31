"""`usher.db.staging` against real Postgres.

`tests/integration/test_bulk_repository.py` already proves the helper works
end to end -- it is the path all four of `PostgresBulkCatalogRepository`'s
writes take -- so this file only pins the two properties a *new* caller
(M4's `media_items` and `watch_states`) has to know and cannot read off that
suite: the staging table is dropped and recreated per batch, and it carries
no constraints, so a value that violates the destination's CHECK survives
the `COPY` and fails one statement later.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.staging import raw_connection, stage_records

_DDL = "CREATE UNLOGGED TABLE stg_probe (n integer, label text)"


async def test_a_staging_table_is_recreated_per_batch(session: AsyncSession) -> None:
    """The caller commits between batches, so a leftover table from a
    crashed batch would otherwise merge into the next one -- silently
    doubling a batch rather than failing."""
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(1, "a")]
    )
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(2, "b")]
    )
    rows = (await session.execute(text("SELECT n, label FROM stg_probe"))).all()
    assert [(row.n, row.label) for row in rows] == [(2, "b")]


async def test_a_destination_check_is_not_enforced_by_the_copy(session: AsyncSession) -> None:
    """The plan this was built from says "CHECK constraints fire during
    `COPY`, so one bad row aborts its batch", which is true of a `COPY`
    straight into a constrained table and **not** true of this path: these
    staging tables are declared without constraints, so the violating value
    lands in staging and only fails at the `INSERT ... SELECT` that follows.

    That difference decides which exception a repository has to catch.
    `copy_records_to_table` runs on the raw asyncpg connection, outside
    SQLAlchemy's error translation, so a CHECK firing there would surface as
    `asyncpg.exceptions.CheckViolationError`; the follow-up statement goes
    through `session.execute`, so it surfaces as
    `sqlalchemy.exc.IntegrityError`. `PostgresMediaItemRepository` catches
    the latter, and this is why that is sufficient.
    """
    await stage_records(
        session,
        ddl="CREATE UNLOGGED TABLE stg_probe (width integer)",
        table="stg_probe",
        columns=("width",),
        records=[(-1,)],
    )
    assert (await session.execute(text("SELECT width FROM stg_probe"))).scalar_one() == -1


async def test_the_raw_connection_is_the_asyncpg_driver(session: AsyncSession) -> None:
    """Two unwrapping layers deep and typed `Any` on the way out, so nothing
    static catches this going stale across a SQLAlchemy upgrade."""
    driver = await raw_connection(session)
    assert type(driver).__module__.startswith("asyncpg")
    assert hasattr(driver, "copy_records_to_table")
