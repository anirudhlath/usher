"""What `usher.db.staging` costs two callers running at the same instant."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest

# Long enough that a blocked `CREATE TABLE` is unmistakably blocked and short
# enough that a failing case reports in seconds. The whole point of these
# cases is a wait that is *not* incidental to a busy host.
_HOLD_SECONDS = 0.8


def _row(key: str) -> JobRequest:
    """A one-row enqueue, the shape the ingest hot path uses.

    Each racer gets its own key: two sessions enqueueing the *same* `(kind, key)`
    genuinely conflict, the second blocking on the first's uncommitted row, and in a
    wall clock that is indistinguishable from the table-level wait these cases are
    about.
    """
    return JobRequest(kind=JobKind.INDEX, key=key, priority=JobPriority.NEW)


@pytest_asyncio.fixture
async def backends(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory whose sessions really commit.

    Not the module-wide `session` fixture: that one is a connection-bound
    transaction rolled back afterwards, so it can neither hold a lock a
    second backend sees nor leave a table behind for case 3.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM jobs"))
            await cleanup.execute(text("DROP TABLE IF EXISTS public.stg_jobs"))
            await cleanup.commit()
        await engine.dispose()


async def _enqueue_and_hold(session: AsyncSession, key: str, barrier: asyncio.Barrier) -> float:
    """Enqueue one row, then hold the transaction open before committing.

    Returns the wall-clock milliseconds the enqueue itself took. The hold comes after
    the timing deliberately: `ACCESS EXCLUSIVE` is held to commit,
    so what a second caller waits on is this session's *whole* transaction,
    and a hold shorter than the wait would hide exactly that.
    """
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)
    # Open the transaction and take a snapshot before the barrier, so the only
    # thing overlapping is the staging call itself. The shape
    # `tests/integration/test_job_queue.py`'s claim harness established.
    await session.execute(text("SELECT 1"))
    await barrier.wait()
    started = time.monotonic()
    await queue.enqueue([_row(key)])
    elapsed = (time.monotonic() - started) * 1000
    await asyncio.sleep(_HOLD_SECONDS)
    await session.commit()
    return elapsed


async def _race(
    factory: async_sessionmaker[AsyncSession],
) -> list[float | BaseException]:
    barrier = asyncio.Barrier(2)
    sessions = [factory(), factory()]
    try:
        return await asyncio.gather(
            *(
                _enqueue_and_hold(one, f"t9900012{index}", barrier)
                for index, one in enumerate(sessions)
            ),
            return_exceptions=True,
        )
    finally:
        for one in sessions:
            await one.close()


async def test_two_concurrent_enqueues_do_not_race_on_the_type_catalogue(
    backends: async_sessionmaker[AsyncSession],
) -> None:
    """The wrong implementation: `CREATE UNLOGGED TABLE stg_jobs` in `public`.

    With no leftover table the failure is not a wait at all: two backends creating the
    same public name at the same instant race on `pg_type_typname_nsp_index`, and the
    loser's `UniqueViolationError` reaches a repository as `IntegrityError` --
    indistinguishable from a duplicate `(kind, key)`, so a healthy batch is reported to
    its caller as a data conflict.
    """
    outcomes = await _race(backends)
    raised = [one for one in outcomes if isinstance(one, BaseException)]
    assert not raised, f"a concurrent one-row enqueue raised {raised!r}"


async def test_a_leftover_public_staging_table_cannot_serialise_two_enqueues(
    backends: async_sessionmaker[AsyncSession],
) -> None:
    """The wrong implementation: any `DROP`/`CREATE` on a name in `public`."""
    async with backends() as setup:
        await setup.execute(text("DROP TABLE IF EXISTS public.stg_jobs"))
        await setup.execute(text("CREATE UNLOGGED TABLE public.stg_jobs (sentinel integer)"))
        await setup.commit()

    outcomes = await _race(backends)
    raised = [one for one in outcomes if isinstance(one, BaseException)]
    assert not raised, f"a concurrent one-row enqueue raised {raised!r}"
    waits = [one for one in outcomes if isinstance(one, float)]
    assert max(waits) < _HOLD_SECONDS * 500, (
        f"one enqueue waited {max(waits):.0f} ms for the other's transaction; "
        "the staging table is shared"
    )

    async with backends() as check:
        survived = (
            await check.execute(text("SELECT to_regclass('public.stg_jobs') IS NOT NULL"))
        ).scalar_one()
    assert survived, "the leftover public table was dropped -- that drop is the shared lock"


async def test_a_committed_enqueue_leaves_no_table_in_the_public_schema(
    backends: async_sessionmaker[AsyncSession],
) -> None:
    """The wrong implementation: a caller that commits leaves the staging table behind.

    Postgres DDL is transactional, so the leftover is invisible under this suite's
    rolled-back isolation and surfaces as schema drift in a later file. A temporary
    table cannot do this: `ON COMMIT DROP` removes it at the commit that would otherwise
    have persisted it, and `inspect(conn).get_table_names()` never saw it at all.
    """
    async with backends() as writer:
        queue = PostgresJobQueue(writer, max_attempts=5, backoff_seconds=30.0)
        assert await queue.enqueue([_row("t99000123")]) == 1
        await writer.commit()

    async with backends() as check:
        leftover = (
            (
                await check.execute(
                    text(
                        "SELECT relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relname LIKE 'stg\\_%'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert leftover == [], f"a committed enqueue left {leftover} in public"


@pytest.mark.parametrize("rows", [1, 5])
async def test_staging_is_idempotent_within_one_transaction(
    backends: async_sessionmaker[AsyncSession], rows: int
) -> None:
    """`ON COMMIT DROP` drops at commit, and a caller may stage twice before one.

    `IngestService` enqueues match jobs and then watch-history jobs against the same
    session, so the second `CREATE TEMP TABLE` meets the first still standing: the
    `DROP TABLE IF EXISTS` has to resolve to the temporary one, or the second call
    raises `DuplicateTableError`.
    """
    async with backends() as writer:
        queue = PostgresJobQueue(writer, max_attempts=5, backoff_seconds=30.0)
        keys = [f"t9900{index:04d}" for index in range(rows)]
        first = await queue.enqueue(
            [JobRequest(kind=JobKind.INDEX, key=key, priority=JobPriority.NEW) for key in keys]
        )
        second = await queue.enqueue(
            [JobRequest(kind=JobKind.INDEX, key=key, priority=JobPriority.DEMAND) for key in keys]
        )
        await writer.commit()
    assert (first, second) == (rows, rows)
