"""`usher.db.staging` against real Postgres."""

import asyncpg.exceptions
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.staging import raw_connection, stage_records

_DDL = "CREATE TEMP TABLE stg_probe (n integer, label text) ON COMMIT DROP"


async def test_a_staging_table_is_recreated_per_batch(session: AsyncSession) -> None:
    """A leftover table from a crashed batch would merge into the next one, doubling it."""
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(1, "a")]
    )
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(2, "b")]
    )
    rows = (await session.execute(text("SELECT n, label FROM stg_probe"))).all()
    assert [(row.n, row.label) for row in rows] == [(2, "b")]


async def test_a_destination_check_is_not_enforced_by_the_copy(session: AsyncSession) -> None:
    """Staging tables carry no constraints, so the `COPY` enforces the destination's none.

    The violating value lands in staging and fails only at the `INSERT ... SELECT` that
    follows, which runs through `session.execute` and so surfaces as
    `sqlalchemy.exc.IntegrityError` — the exception `PostgresMediaItemRepository`
    catches.
    """
    await stage_records(
        session,
        ddl="CREATE TEMP TABLE stg_probe (width integer) ON COMMIT DROP",
        table="stg_probe",
        columns=("width",),
        records=[(-1,)],
    )
    assert (await session.execute(text("SELECT width FROM stg_probe"))).scalar_one() == -1


async def test_the_raw_connection_is_the_asyncpg_driver(session: AsyncSession) -> None:
    """Two unwrapping layers deep and typed `Any`, so nothing static catches it going stale."""
    driver = await raw_connection(session)
    assert type(driver).__module__.startswith("asyncpg")
    assert hasattr(driver, "copy_records_to_table")


# ---------------------------------------------------------------------------
# The COPY path's two failure shapes, observed rather than asserted
# ---------------------------------------------------------------------------


async def test_the_copy_refuses_an_over_long_string_server_side_as_22001(
    session: AsyncSession,
) -> None:
    """The refusal is raw asyncpg: not a `DBAPIError`, with no `.orig` chain to read."""
    with pytest.raises(asyncpg.exceptions.StringDataRightTruncationError) as caught:
        await stage_records(
            session,
            ddl="CREATE TEMP TABLE stg_probe (container varchar(32)) ON COMMIT DROP",
            table="stg_probe",
            columns=("container",),
            records=[("x" * 33,)],
        )

    assert caught.value.sqlstate == "22001"
    assert not isinstance(caught.value, DBAPIError)
    # There is no `.orig` chain to read a SQLSTATE off, which is the mechanical
    # statement of "outside SQLAlchemy's error translation".
    assert not hasattr(caught.value, "orig")


async def test_the_copy_refuses_an_out_of_range_integer_with_no_sqlstate_at_all(
    session: AsyncSession,
) -> None:
    """The other failure shape carries no SQLSTATE at all.

    An out-of-range `int` never reaches Postgres: asyncpg's binary encoder refuses it
    client-side as a bare `builtins.OverflowError`, which is neither a `DBAPIError` nor
    an `asyncpg.exceptions.PostgresError`, so no single `except` clause covers both
    shapes.
    """
    with pytest.raises(OverflowError) as caught:
        await stage_records(
            session,
            ddl="CREATE TEMP TABLE stg_probe (n integer) ON COMMIT DROP",
            table="stg_probe",
            columns=("n",),
            records=[(2**31,)],
        )

    assert not isinstance(caught.value, DBAPIError | asyncpg.exceptions.PostgresError)
    assert getattr(caught.value, "sqlstate", None) is None


async def test_a_bigint_staging_column_takes_the_same_value_the_integer_one_refused(
    session: AsyncSession,
) -> None:
    """The control for widening a staging column to `bigint`.

    The same `2**31` that aborts an `integer` batch lands in a `bigint` column and reads
    back unchanged, so the widening rests on a positive rather than on an absence.
    """
    await stage_records(
        session,
        ddl="CREATE TEMP TABLE stg_probe (n bigint) ON COMMIT DROP",
        table="stg_probe",
        columns=("n",),
        records=[(2**31,)],
    )
    assert (await session.execute(text("SELECT n FROM stg_probe"))).scalar_one() == 2**31
