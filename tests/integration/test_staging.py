"""`usher.db.staging` against real Postgres."""

import asyncpg.exceptions
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.staging import raw_connection, stage_records

_DDL = "CREATE TEMP TABLE stg_probe (n integer, label text) ON COMMIT DROP"


async def test_a_staging_table_is_recreated_per_batch(session: AsyncSession) -> None:
    """The caller commits between batches.

    so a leftover table from a crashed batch would otherwise merge into the next one --
    silently doubling a batch rather than failing.
    """
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(1, "a")]
    )
    await stage_records(
        session, ddl=_DDL, table="stg_probe", columns=("n", "label"), records=[(2, "b")]
    )
    rows = (await session.execute(text("SELECT n, label FROM stg_probe"))).all()
    assert [(row.n, row.label) for row in rows] == [(2, "b")]


async def test_a_destination_check_is_not_enforced_by_the_copy(session: AsyncSession) -> None:
    """The plan this was built from says "CHECK constraints fire during `COPY`.

    so one bad row aborts its batch", which is true of a `COPY` straight into a
    constrained table and **not** true of this path: these staging tables are declared
    without constraints, so the violating value lands in staging and only fails at the
    `INSERT ...

    SELECT` that follows.

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
        ddl="CREATE TEMP TABLE stg_probe (width integer) ON COMMIT DROP",
        table="stg_probe",
        columns=("width",),
        records=[(-1,)],
    )
    assert (await session.execute(text("SELECT width FROM stg_probe"))).scalar_one() == -1


async def test_the_raw_connection_is_the_asyncpg_driver(session: AsyncSession) -> None:
    """Two unwrapping layers deep and typed `Any` on the way out.

    so nothing static catches this going stale across a SQLAlchemy upgrade.
    """
    driver = await raw_connection(session)
    assert type(driver).__module__.startswith("asyncpg")
    assert hasattr(driver, "copy_records_to_table")


# -------------------------------------------------------------------------- ADR-0044's
# two COPY-path failure shapes, observed rather than asserted
# --------------------------------------------------------------------------
# [ADR-0044](../../docs/prd/decisions/0044-a-bounded-column-is-a-declared-type-that-
# refuses.md) closes its Evidence with 🔴 *"the `22001` server-side COPY refusal is the


async def test_the_copy_refuses_an_over_long_string_server_side_as_22001(
    session: AsyncSession,
) -> None:
    """The shape ADR-0044 predicted and had not run.

    It predicts correctly.
    """
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
    """The *other* of ADR-0044's two shapes.

    and the reason it says a fix that widens `bigint` and forgets `text` reaches 28 of
    31 rather than all of them.

    An out-of-range `int` never reaches Postgres: asyncpg's binary encoder
    refuses it client-side as a bare `builtins.OverflowError`, which has no
    `sqlstate` attribute, is not a `DBAPIError` and is not even an
    `asyncpg.exceptions.PostgresError`. Recorded beside the `22001` case
    because the two are the same bucket and need different repairs — and
    because reading either one alone makes the COPY path look like something
    an `except` clause could cover.
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
    """The control for ADR-0044's scope item 2.

    which widens `stg_genome.tmdb_id` and `stg_akas.ordering` to `bigint`.

    Without this the widening's whole claim — that the value now *stages* —
    rests on the two cases above failing, which is an absence. The same
    `2**31` that aborts an `integer` batch lands in a `bigint` column and reads
    back unchanged.
    """
    await stage_records(
        session,
        ddl="CREATE TEMP TABLE stg_probe (n bigint) ON COMMIT DROP",
        table="stg_probe",
        columns=("n",),
        records=[(2**31,)],
    )
    assert (await session.execute(text("SELECT n FROM stg_probe"))).scalar_one() == 2**31
