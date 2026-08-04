"""What `usher.db.staging` costs two callers running at the same instant.

M5 recorded the contention and M6 is what makes it hurt: `EnrichService`
enqueues one `index` job per enriched title, so a single-row `COPY` runs on
the pipeline's hot path against the same staging name a nightly walk's batch
is using. Every case here needs **two real backends that commit**, which is
why none of them can use the rolled-back `session` fixture.

The three failures these pin are different failures, and only the second is
the one the plan predicted:

1. **Two concurrent stagers with no leftover table do not wait -- they
   raise.** `CREATE UNLOGGED TABLE stg_jobs` in two sessions at once races on
   `pg_type_typname_nsp_index`, which asyncpg reports as
   `UniqueViolationError` and SQLAlchemy wraps as `IntegrityError`. A
   repository whose `except IntegrityError` means "a genuine data conflict"
   cannot tell the two apart, so a perfectly healthy batch is reported to its
   caller as a constraint violation.
2. **Two concurrent stagers *with* a leftover table serialise for the length
   of the other's whole transaction**, not for the length of a DDL, because
   `ACCESS EXCLUSIVE` is held to commit.
3. **A committed staging call leaves a table behind in `public`**, which
   surfaces as schema drift in `test_migration_matches_the_orm_metadata` --
   in a *later file*, so the suite that caused it passes alone. Nine
   integration files carried an explicit `DROP TABLE IF EXISTS stg_*` for
   this; a temporary table deletes the need for all nine.

The fix is `CREATE TEMP TABLE ... ON COMMIT DROP`: a name in the session's
own `pg_temp` schema, so there is nothing shared to lock, nothing shared to
race on, and nothing left at commit.
"""

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
    """A one-row enqueue, which is what M6 put on the hot path.

    **Each racer gets its own key, and that is not tidiness.** Two sessions
    enqueueing the *same* `(kind, key)` genuinely conflict: the second's
    `INSERT ... ON CONFLICT` blocks on the first's uncommitted row until it
    ends, which is Postgres doing its job and is indistinguishable, in a wall
    clock, from the table-level wait these cases exist to measure. Written
    with one shared key first and it produced an 816 ms wait against a fixed
    implementation -- a case that would have passed for the wrong reason
    before the fix and failed for the wrong reason after it.
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

    Returns the wall-clock milliseconds the enqueue itself took. The hold is
    after the measurement deliberately: `ACCESS EXCLUSIVE` is held to commit,
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
    """The wrong implementation: `CREATE UNLOGGED TABLE stg_jobs`, today's.

    With **no** leftover table the failure is not a wait at all. Two backends
    creating the same public name at the same instant race on
    `pg_type_typname_nsp_index` and one gets
    `asyncpg.exceptions.UniqueViolationError`, which reaches a repository as
    `sqlalchemy.exc.IntegrityError` -- indistinguishable from
    `ck_jobs_key_not_empty` or a duplicate `(kind, key)`. So a healthy batch
    is reported to its caller as a data conflict, and the *only* thing wrong
    with it was the instant it ran.

    Measured on this host before the fix: `duplicate key value violates
    unique constraint "pg_type_typname_nsp_index"`, `Key (typname,
    typnamespace)=(stg_jobs, 2200)`.
    """
    outcomes = await _race(backends)
    raised = [one for one in outcomes if isinstance(one, BaseException)]
    assert not raised, f"a concurrent one-row enqueue raised {raised!r}"


async def test_a_leftover_public_staging_table_cannot_serialise_two_enqueues(
    backends: async_sessionmaker[AsyncSession],
) -> None:
    """The wrong implementation: any `DROP`/`CREATE` on a name in `public`.

    A leftover `public.stg_jobs` -- from a crashed batch, or from a release
    that predates the temporary tables -- is the state in which today's code
    does not raise but *waits*. Both statements take `ACCESS EXCLUSIVE` and
    both are held to commit, so the second caller waits for the length of the
    first caller's whole transaction. Measured at 813 ms against an 800 ms
    hold, in lockstep.

    Asserted on the measured wait rather than on a lock row, because the
    property is "the second caller was not made to wait" and a lock row is
    one implementation's evidence for it. `_HOLD_SECONDS / 2` is the
    threshold: a wait caused by this is *at least* the whole hold, and
    nothing else here costs 400 ms.

    Also asserts the leftover is left alone. `DROP TABLE IF EXISTS
    pg_temp.stg_jobs` is what makes a leftover harmless rather than merely
    unlikely, and an unqualified drop would take the shared lock exactly once
    -- which is a 813 ms stall an operator sees once per deployment and can
    never reproduce.
    """
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
    """The wrong implementation: today's, and the reason nine test files
    carry a `DROP TABLE IF EXISTS stg_*` line.

    Postgres DDL is transactional, so a caller that *commits* leaves the
    staging table behind. That is invisible in this suite's usual
    rolled-back-transaction isolation and shows up as schema drift in
    `test_migration_matches_the_orm_metadata` -- in a later file, so the
    suite that caused it passes alone and takes the migration test down in
    combination. A temporary table cannot do this: `ON COMMIT DROP` removes
    it at the commit that would otherwise have persisted it, and
    `inspect(conn).get_table_names()` never saw it in the first place.
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
    """`ON COMMIT DROP` drops at commit, and a caller may stage twice before
    one.

    `IngestService` enqueues match jobs and then watch-history jobs against
    the same session before its batch commit, so the second `CREATE TEMP
    TABLE` meets the first one still standing. The `DROP TABLE IF EXISTS`
    that made that work for a public table has to keep working for a
    temporary one -- and it has to resolve to the temporary one, or the
    second call raises `DuplicateTableError`.
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
