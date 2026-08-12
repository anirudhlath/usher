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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.contract.job_queue_contract import ClaimWindow, overlapping
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.domain.jobs import Job, JobKind, JobPriority
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.services.events import DeferredEventPublisher
from usher.services.jobs import DEFAULT_LEASE_SECONDS, JobScope, JobWorker

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


def _worker(
    sessions: async_sessionmaker[AsyncSession],
    handlers: Mapping[JobKind, Callable[[Job], Awaitable[None]]],
    *,
    concurrency: int = 1,
    batch_size: int = 20,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> JobWorker:
    """A worker in the shape a composition root builds: **a session per scope.**

    This is the production wiring rather than a convenience. `AsyncSession` is
    not concurrency-safe, so `JobWorker` opens one scope for the claim and one
    per job, and `composition.build_worker`'s factory opens a session inside
    each. The unit file's equivalent hands back one pipeline over fakes every
    time and says so; here the sessions really are different, which is the only
    place that difference can be *observed* -- see
    `test_two_jobs_in_flight_at_once_hold_different_connections` below.
    """

    @asynccontextmanager
    async def _scope() -> AsyncIterator[JobScope]:
        async with sessions() as session:
            yield JobScope(
                queue=_queue(session),
                commit=session.commit,
                handlers=handlers,
                events=DeferredEventPublisher(NullEventPublisher()),
            )

    return JobWorker(
        _scope,
        dict.fromkeys(handlers, concurrency),
        max_in_flight=concurrency,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
    )


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

    async def _handle(job: Job) -> None:
        seen.append(await _status_of(factory, job.key))

    worker = _worker(factory, {JobKind.ENRICH: _handle})
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
        worker = _worker(factory, {JobKind.ENRICH: _recorder(handled[index])}, batch_size=2)
        async with factory() as session:
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
    """`recover()` requeues `running` and must not touch `parked`.

    A requeue keyed on anything looser un-parks poison on every restart,
    which is the failure parking exists to end arriving through the recovery
    path -- and it fails silently, because the job simply starts being
    retried again.
    """
    await _enqueue(factory, "t1")
    poison = {JobKind.ENRICH: _raising(PortDataMalformed("the answer was wrong"))}
    await _worker(factory, poison).run_once()
    assert await _status_of(factory, "t1") == "parked"

    restarted = _worker(factory, poison, lease_seconds=0.0)
    assert await restarted.recover() == 0
    assert await restarted.run_once() == 0
    assert await _status_of(factory, "t1") == "parked"


async def test_recover_takes_back_a_claim_a_killed_worker_committed(
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
    # `lease_seconds=0.0` because the claim above was made milliseconds ago and
    # the shipped 300 s lease is what stops a worker recovering work somebody
    # is still doing. The *other* arm -- that a claim inside its lease is left
    # alone -- is
    # `test_a_live_workers_claim_survives_another_workers_recovery` below, and
    # it is the one with teeth: "an abandoned claim comes back" is satisfied by
    # requeueing everything, which is exactly what this replaced.
    restarted = _worker(factory, {JobKind.ENRICH: _recorder(handled)}, lease_seconds=0.0)
    assert await restarted.recover() == 1
    assert await restarted.run_once() == 1
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
    down = _worker(factory, {JobKind.ENRICH: _raising(PortUnavailable("upstream is down"))})
    assert await down.run_once() == 1
    assert await _status_of(factory, "t1") == "pending"

    other = _worker(factory, {JobKind.ENRICH: _recorder([])})
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
    worker = _worker(factory, {JobKind.WATCH_WRITEBACK: _recorder(handled)})
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


# -- the per-job scope, and what only a real engine can say about it ---------


async def test_two_jobs_in_flight_at_once_hold_different_connections(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """**The claim W1 rests on, measured rather than argued.**

    `AsyncSession` is not concurrency-safe and every repository a handler holds
    is bound to one, so "the worker runs jobs concurrently" is only safe if each
    job got a session of its own. Nothing in the unit suite can see that: its
    scope factory hands back one pipeline over fakes every time, deliberately,
    because a dict has no connection to be different.

    Here each job reads Postgres's own `pg_backend_pid()` **through the queue's
    session**, so the value is a property of the object the handler's
    repositories would use, not of anything the case constructed. Two distinct
    pids is what a per-job scope produces; one is what a shared session
    produces, and one is also what a *serialised* worker produces -- which is
    why the overlap is asserted beside it rather than instead of it.
    """
    await _enqueue(factory, "a", "b")
    rendezvous = asyncio.Barrier(2)
    pids: dict[str, int] = {}
    windows: list[ClaimWindow] = []
    seen: dict[str, AsyncSession] = {}

    async def _handle(job: Job) -> None:
        started = time.monotonic()
        session = seen[job.key]
        pids[job.key] = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        await asyncio.wait_for(rendezvous.wait(), CLAIM_TIMEOUT)
        windows.append(
            ClaimWindow(keys=(job.key,), started_at=started, finished_at=time.monotonic())
        )

    @asynccontextmanager
    async def _scope() -> AsyncIterator[JobScope]:
        async with factory() as session:

            async def _bind(job: Job) -> None:
                seen[job.key] = session
                await _handle(job)

            yield JobScope(
                queue=_queue(session),
                commit=session.commit,
                handlers={JobKind.ENRICH: _bind},
                events=DeferredEventPublisher(NullEventPublisher()),
            )

    worker = JobWorker(_scope, {JobKind.ENRICH: 2}, max_in_flight=2)
    assert await asyncio.wait_for(worker.run_once(), CLAIM_TIMEOUT) == 2

    assert set(pids) == {"a", "b"}, f"the premise: both handlers ran -- {pids}"
    assert overlapping(windows), f"the two jobs did not overlap: {windows}"
    assert pids["a"] != pids["b"], (
        "two concurrently-running jobs shared one Postgres backend, so they shared "
        f"one AsyncSession: {pids}"
    )


async def test_two_concurrent_jobs_on_one_shared_session_really_do_break(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The positive control for the case above, and the honest limit of what
    this task can claim about `MissingGreenlet`.

    M9's S3 lost a worker to an unhandled
    `MissingGreenlet: greenlet_spawn has not been called`, and the hypothesis
    W1 was dispatched with is that it was an `AsyncSession` touched from the
    wrong context. **This case does not reproduce that crash and is not
    evidence that it is fixed** -- see the write-up in
    `.claude/rules/tmdb-and-enrichment.md`, which records why the shape of that
    run refutes the shared-session explanation outright. What it does establish
    is that the hazard the per-job scope removes is real and not theoretical:
    with one session behind two concurrent jobs, SQLAlchemy raises rather than
    silently interleaving, and it raises from the same family.

    The failure is recorded rather than asserted by name, because which member
    of the family arrives depends on which of the two coroutines gets there
    first, and a case pinned to one spelling would be flaky by construction.
    """
    await _enqueue(factory, "a", "b")
    rendezvous = asyncio.Barrier(2)
    failures: list[str] = []

    async with factory() as shared:

        async def _handle(job: Job) -> None:
            # Both jobs park here, then both drive the *same* session at once.
            await asyncio.wait_for(rendezvous.wait(), CLAIM_TIMEOUT)
            await shared.execute(text("SELECT pg_sleep(0.05)"))

        @asynccontextmanager
        async def _scope() -> AsyncIterator[JobScope]:
            yield JobScope(
                queue=_queue(shared),
                commit=shared.commit,
                handlers={JobKind.ENRICH: _handle},
                events=DeferredEventPublisher(NullEventPublisher()),
            )

        worker = JobWorker(_scope, {JobKind.ENRICH: 2}, max_in_flight=2)
        try:
            await asyncio.wait_for(worker.run_once(), CLAIM_TIMEOUT)
        except BaseException as exc:
            failures.append(f"{type(exc).__module__}.{type(exc).__name__}: {exc}")

    assert failures, (
        "two concurrent jobs drove one AsyncSession and nothing complained -- "
        "either the worker serialised them (so the premise is gone) or SQLAlchemy "
        "silently interleaved two statements on one connection"
    )
    print(f"one shared session under two concurrent jobs raises: {failures[0]}")


async def test_a_live_workers_claim_survives_another_workers_recovery(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """**The arm that makes recovery a lease rather than a theft**, against
    two real backends.

    M9's S3: one of three workers died holding twenty claims, and the only
    recovery lever was `requeue_running(older_than_seconds=0.0)` -- which would
    have taken the other two workers' live claims with it. *"A genuine dead end
    at N > 1"*, and the twenty were written off.

    So the property is the negative one: a second worker recovering orphans
    must leave a claim somebody is still working on exactly where it is. The
    premise is asserted first -- a `running` row has to exist for the recovery
    to have had the chance to steal it.
    """
    await _enqueue(factory, "held")
    async with factory() as live:
        claimed = await asyncio.wait_for(_queue(live).claim([JobKind.ENRICH]), CLAIM_TIMEOUT)
        assert [job.key for job in claimed] == ["held"], "the premise: a live claim exists"
        await live.commit()
        assert await _status_of(factory, "held") == "running"

        peer = _worker(factory, {JobKind.ENRICH: _recorder([])})
        assert await peer.recover() == 0
        assert await _status_of(factory, "held") == "running", (
            "a second worker's recovery stole a claim its holder was still working on"
        )

        # And the heartbeat keeps it there: the beat is what lets the lease be
        # minutes rather than longer than the longest job.
        assert await _queue(live).touch([claimed[0].id]) == 1
        await live.commit()
        assert await _status_of(factory, "held") == "running"

    # The control that makes the two assertions above mean something: past the
    # lease, the same call does take it.
    aged = _worker(factory, {JobKind.ENRICH: _recorder([])}, lease_seconds=0.0)
    assert await aged.recover() == 1
    assert await _status_of(factory, "held") == "pending"


async def test_a_touch_does_not_resurrect_a_job_another_worker_recovered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`_TOUCH`'s `status = 'running'` predicate, which is the half that is
    easy to leave out.

    A beat is sent for everything a worker holds in flight, and by the time it
    lands a peer may already have recovered the claim (the row is `pending`
    again) or the job may have been parked. Without the predicate the beat
    moves `updated_at` on those rows too -- which for a `pending` row is
    harmless noise and for a `parked` one is a lie in the column an operator
    sorts the review queue by.
    """
    await _enqueue(factory, "recovered", "poisoned")
    async with factory() as session:
        queue = _queue(session)
        claimed = await asyncio.wait_for(queue.claim([JobKind.ENRICH], limit=2), CLAIM_TIMEOUT)
        assert len(claimed) == 2, "the premise: two live claims"
        assert await queue.requeue_running() == 2
        parked = await queue.fail(claimed[0].id, error="the answer was wrong", retryable=False)
        assert parked is not None and parked.status.value == "parked", "the premise: one parked"
        await session.commit()

        assert await queue.touch([job.id for job in claimed]) == 0, (
            "the heartbeat moved rows no worker is holding"
        )
