"""`JobWorker` against real Postgres, for the two things `FakeJobQueue`
cannot model at all.

Its own module docstring puts `SKIP LOCKED` first among them, and the worker
is the code that depends on it most: `test_two_workers_never_claim_the_same_job`
is *skipped* for the fake rather than passed, so every unit case about
claiming runs against a store where contention is structurally impossible.

1. **The claim is durable, not merely ordered.** The unit suite asserts that
   a commit happened before the first handler; nothing there can tell that
   from a no-op, because a dict has no transaction. Here a second Postgres
   backend reads the row *while the handler is still running* and has to see
   `running`. Move the worker's commit after the loop and it reads `pending`
   -- which is what a restart's `requeue_running` would find, and what makes
   "a killed worker's claims are recoverable" false.
2. **Two workers split a batch.** Released through an `asyncio.Barrier` and
   asserted on measured overlap rather than on a count -- "each worker ran
   two jobs" is also what a serialised pair produces, which is the M3 failure
   this project already had once.

Every claim is bounded by `asyncio.wait_for`, for the reason
`tests/integration/test_job_queue.py` states: the wrong spellings of the
claim do not answer wrongly, they block forever, and a test that hangs
reports nothing.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.domain.jobs import Job, JobKind, JobPriority
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.jobs import JobRequest
from usher.services.jobs import JobWorker

# Generous next to a local claim (single-digit milliseconds), short next to a
# test run. Anything that reaches it is blocked on a lock, not slow.
CLAIM_TIMEOUT = 5.0


@pytest_asyncio.fixture
async def factory(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Engine-bound sessions, because these cases need genuinely separate
    Postgres backends.

    The shared `session` fixture is one connection inside one externally
    managed transaction that is rolled back afterwards, so two workers over
    it would be *one* backend and could not contend for a row lock even in
    principle. These commit, so they clean up after themselves.

    **No `DROP TABLE IF EXISTS stg_jobs` any more.** It used to be here
    because Postgres DDL is transactional and a committing test was the one
    shape that left a staging table behind, which surfaced as schema drift in
    `test_migration_matches_the_orm_metadata` in a *later* file. M6 made
    `usher.db.staging` create `CREATE TEMP TABLE ... ON COMMIT DROP`, so the
    commit below is what removes the table rather than what persists it, and
    a cleanup that can no longer fire is indistinguishable from one still
    needed.
    """
    engine = build_engine(postgres_url)
    make = build_session_factory(engine)
    try:
        yield make
    finally:
        async with make() as cleanup:
            await cleanup.execute(text("DELETE FROM jobs"))
            await cleanup.commit()
        await engine.dispose()


def _queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=3, backoff_seconds=30.0)


async def _enqueue(factory: async_sessionmaker[AsyncSession], *keys: str) -> None:
    async with factory() as writer:
        await _queue(writer).enqueue(
            [JobRequest(kind=JobKind.ENRICH, key=key, priority=JobPriority.NEW) for key in keys]
        )
        # Committed, or the workers' own backends cannot see the rows at all
        # and every claim trivially returns nothing.
        await writer.commit()


async def _status_of(factory: async_sessionmaker[AsyncSession], key: str) -> str | None:
    """What a *different* backend can see about a job right now."""
    async with factory() as observer:
        return (
            await observer.execute(
                text("SELECT status FROM jobs WHERE kind = 'enrich' AND key = :key"),
                {"key": key},
            )
        ).scalar_one_or_none()


async def test_the_claim_is_durable_while_the_handler_runs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The property the unit suite can only approximate.

    A worker that claims and works inside one transaction has written
    nothing another process can see, so a restart's `requeue_running` finds
    no `running` row to recover and the job is indistinguishable from one
    nobody ever tried. It also means the claim's locks are held for the
    length of the slowest upstream call rather than for the length of a
    claim.
    """
    await _enqueue(factory, "t1")
    seen: list[str | None] = []

    async with factory() as worker_session:
        worker = JobWorker(queue=_queue(worker_session), commit=worker_session.commit)

        async def _handle(job: Job) -> None:
            seen.append(await _status_of(factory, job.key))

        worker.register(JobKind.ENRICH, _handle)
        assert await asyncio.wait_for(worker.run_once(), CLAIM_TIMEOUT) == 1

    assert seen == ["running"], "another backend could not see the claim while it ran"
    assert await _status_of(factory, "t1") is None, "the completed job was not deleted"


async def test_two_workers_split_one_batch_and_never_run_a_job_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`SKIP LOCKED` seen from the layer that depends on it.

    The overlap assertion is the one with teeth: "each worker ran two of the
    four" is also what a serialised pair produces, and a claim that
    serialises (bare `FOR UPDATE`) or duplicates (no locking clause at all)
    is a correctness failure the counts alone would ratify. The M3 failure
    this project already had was exactly that shape -- a deleted
    single-flight lock whose concurrency test passed five runs in a row.
    """
    await _enqueue(factory, "t1", "t2", "t3", "t4")
    barrier = asyncio.Barrier(2)
    windows: list[tuple[float, float]] = []
    handled: dict[int, list[str]] = {0: [], 1: []}

    async def _run(index: int) -> None:
        async with factory() as session:
            worker = JobWorker(queue=_queue(session), commit=session.commit, batch_size=2)
            worker.register(JobKind.ENRICH, _recorder(handled[index]))
            # Opens the transaction and takes a snapshot before the barrier,
            # so the only thing overlapping is the claim itself.
            await session.execute(text("SELECT 1"))
            await barrier.wait()
            started = time.monotonic()
            await asyncio.wait_for(worker.run_once(), CLAIM_TIMEOUT)
            windows.append((started, time.monotonic()))

    await asyncio.gather(_run(0), _run(1))

    assert sorted(handled[0] + handled[1]) == ["t1", "t2", "t3", "t4"]
    assert set(handled[0]).isdisjoint(handled[1]), "both workers ran the same job"
    first, second = windows
    overlap = min(first[1], second[1]) - max(first[0], second[0])
    assert overlap > 0, f"the two runs did not overlap at all: {windows}"


def _recorder(into: list[str]) -> Callable[[Job], Awaitable[None]]:
    async def _handle(job: Job) -> None:
        into.append(job.key)
        # Long enough that the two workers' handler phases genuinely overlap
        # rather than one finishing before the other is scheduled.
        await asyncio.sleep(0.05)

    return _handle


async def test_a_parked_job_stays_parked_across_a_restart(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`startup()` requeues `running` and must not touch `parked`.

    A requeue keyed on anything looser un-parks poison on every restart,
    which is the failure parking exists to end arriving through the recovery
    path -- and it fails silently, because the job simply starts being
    retried again.
    """
    await _enqueue(factory, "t1")
    async with factory() as session:
        worker = JobWorker(queue=_queue(session), commit=session.commit)
        worker.register(JobKind.ENRICH, _raising(PortDataMalformed("the answer was wrong")))
        await worker.run_once()
    assert await _status_of(factory, "t1") == "parked"

    async with factory() as restarted:
        worker = JobWorker(queue=_queue(restarted), commit=restarted.commit)
        worker.register(JobKind.ENRICH, _raising(PortDataMalformed("the answer was wrong")))
        assert await worker.startup() == 0
        assert await worker.run_once() == 0
    assert await _status_of(factory, "t1") == "parked"


async def test_startup_recovers_a_claim_a_killed_worker_committed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of "the claim is durable": the recovery that durability
    is *for*. A worker claims, commits, and its process dies before the
    handler returns; the next start has to find the row and hand it back.
    """
    await _enqueue(factory, "t1")
    async with factory() as dying:
        claimed = await asyncio.wait_for(_queue(dying).claim([JobKind.ENRICH]), CLAIM_TIMEOUT)
        assert [job.key for job in claimed] == ["t1"]
        await dying.commit()
    assert await _status_of(factory, "t1") == "running"

    handled: list[str] = []
    async with factory() as restarted:
        worker = JobWorker(queue=_queue(restarted), commit=restarted.commit)
        worker.register(JobKind.ENRICH, _recorder(handled))
        assert await worker.startup() == 1
        assert await worker.run_once() == 1
    assert handled == ["t1"]


async def test_a_transient_failure_is_not_re_claimable_by_a_second_worker_either(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The backoff, from outside the process that set it.

    `FakeJobQueue`'s backoff is deterministic and single-threaded, so
    "nothing re-claims this yet" there is a statement about one dict. Here
    the interval is a real jittered `run_after` on a committed row, and the
    worker asking is a different backend -- which is the shape a pool
    hammering a broken upstream would actually take.
    """
    await _enqueue(factory, "t1")
    async with factory() as first:
        worker = JobWorker(queue=_queue(first), commit=first.commit)
        worker.register(JobKind.ENRICH, _raising(PortUnavailable("upstream is down")))
        assert await worker.run_once() == 1
    assert await _status_of(factory, "t1") == "pending"

    async with factory() as second:
        other = JobWorker(queue=_queue(second), commit=second.commit)
        other.register(JobKind.ENRICH, _recorder([]))
        assert await asyncio.wait_for(other.run_once(), CLAIM_TIMEOUT) == 0

    async with factory() as reader:
        run_after = (
            await reader.execute(text("SELECT run_after FROM jobs WHERE key = 't1'"))
        ).scalar_one()
    assert run_after is not None, "a failed job with no backoff is a hot loop"


async def test_the_newest_kind_stores_claims_and_completes_with_no_migration_behind_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`JobKind.WATCH_WRITEBACK` shipped without a migration, and this is that
    claim measured rather than argued.

    `db/models/jobs.py` declares `kind` through `enum_column(JobKind,
    length=32)`, whose `native_enum=False` compiles to a plain `VARCHAR(32)`
    and whose `create_constraint` defaults to `False` in SQLAlchemy 2.0 -- so
    the database holds no membership CHECK and no native enum type, and
    Pydantic owns membership. Nothing in the unit suite can see any of that:
    `FakeJobQueue` is a dict keyed by `(kind, key)` and would accept a member
    Postgres refuses, whichever way the column had really been declared.

    Three separate things could each have needed a migration and none did:
    the value has to be **storable** (a CHECK would refuse it), **round-trip**
    (a native enum type would need an `ALTER TYPE` and the string would come
    back as something else), and be **claimable** by
    `kind = ANY(:kinds)`. So the assertions are the stored spelling read back
    as raw SQL, the claim, and the deletion -- `watch_writeback` is fifteen
    characters against a bound of thirty-two, which is the other thing a
    silent truncation would break.
    """
    async with factory() as writer:
        await _queue(writer).enqueue(
            [JobRequest(kind=JobKind.WATCH_WRITEBACK, key="emby-1", priority=JobPriority.VISIBLE)]
        )
        await writer.commit()

    async with factory() as reader:
        stored = (
            await reader.execute(text("SELECT kind FROM jobs WHERE key = 'emby-1'"))
        ).scalar_one()
    assert stored == JobKind.WATCH_WRITEBACK.value == "watch_writeback"

    handled: list[str] = []
    async with factory() as session:
        worker = JobWorker(queue=_queue(session), commit=session.commit)
        worker.register(JobKind.WATCH_WRITEBACK, _recorder(handled))
        assert await asyncio.wait_for(worker.run_once(), CLAIM_TIMEOUT) == 1

    assert handled == ["emby-1"]
    async with factory() as after:
        remaining = (
            await after.execute(text("SELECT count(*) FROM jobs WHERE key = 'emby-1'"))
        ).scalar_one()
    assert remaining == 0, "a completed write-back kept its row"


def _raising(exc: BaseException) -> Callable[[Job], Awaitable[None]]:
    async def _handle(job: Job) -> None:
        raise exc

    return _handle
