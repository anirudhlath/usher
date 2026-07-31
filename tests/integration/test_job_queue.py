"""The shared contract against real Postgres -- the half that can lock.

`FakeJobQueue` cannot express `SELECT ... FOR UPDATE SKIP LOCKED` at all, so
`test_two_workers_never_claim_the_same_job` is skipped there and runs here,
against two real backends. Everything the fake's docstring lists as
"forgiving" is closed by one of the cases below: the locking, the jitter, the
`CardinalityViolationError` a batch duplicate causes without
`SELECT DISTINCT ON`, the CHECK constraints, and the poisoned session a
caught conflict leaves behind without a SAVEPOINT.

**Why the concurrency cases use their own engine-bound sessions.** The shared
`session` fixture is a single connection inside one externally-managed
transaction that is rolled back afterward (see `conftest.py`); two
`PostgresJobQueue` instances over it would be *one* Postgres backend and could
not contend for a row lock even in principle. The writer commits, so these
cases clean up after themselves explicitly. Same shape as
`tests/integration/test_bootstrap_concurrency.py`.

**Every claim in these cases is bounded by `asyncio.wait_for`.** A claim
spelled `FOR UPDATE` without `SKIP LOCKED` does not return a wrong answer --
it *blocks*, until the transaction holding the lock ends, which in these
tests is never. A test that hangs reports nothing and a CI run that hangs
reports less, so the timeout turns the blocking failure into an ordinary
assertion failure.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.contract.job_queue_contract import (
    ClaimWindow,
    ClearBackoff,
    ConcurrentClaimHarness,
    JobQueueContract,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.jobs import JobRequest

# Generous relative to a local claim (single-digit milliseconds) and short
# relative to a test run. Anything that reaches it is blocked on a lock, not
# slow.
CLAIM_TIMEOUT = 5.0


@pytest.fixture
def queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)


@pytest_asyncio.fixture
async def clear_backoff(session: AsyncSession) -> ClearBackoff:
    """The Postgres half of the contract's test-only hook -- see
    `tests/contract/job_queue_contract.py`'s docstring for why this is a
    fixture rather than a port method."""

    async def _clear() -> None:
        await session.execute(text("UPDATE jobs SET run_after = NULL"))

    return _clear


class _PostgresConcurrentClaims(ConcurrentClaimHarness):
    """Genuinely overlapping claims across separate Postgres backends.

    `asyncio.Barrier` is what makes the overlap real rather than hoped for:
    every claimer opens its transaction, waits until all of them have, and
    only then issues its claim. Without it the first claimer's whole
    round trip completes before the second is scheduled, which is exactly the
    serialised run whose claim counts look identical and prove nothing.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], sessions: list[AsyncSession]):
        self._factory = factory
        self._sessions = sessions

    def session(self, index: int) -> AsyncSession:
        """One claimer's own backend, for cases that need to drive the two
        sides by hand rather than through `run`."""
        return self._sessions[index]

    async def run(self, *, keys: Sequence[str], claimers: int, limit: int = 1) -> list[ClaimWindow]:
        assert claimers <= len(self._sessions), "not enough sessions for that many claimers"
        async with self._factory() as writer:
            await PostgresJobQueue(writer, max_attempts=5, backoff_seconds=1.0).enqueue(
                [JobRequest(kind=JobKind.ENRICH, key=key, priority=JobPriority.NEW) for key in keys]
            )
            # Committed, or the claimers' own backends cannot see the rows at
            # all and every claim trivially returns nothing.
            await writer.commit()

        barrier = asyncio.Barrier(claimers)

        async def claimer(session: AsyncSession) -> ClaimWindow:
            queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)
            # Opens the transaction and takes a snapshot before the barrier,
            # so the only thing overlapping is the claim itself.
            await session.execute(text("SELECT 1"))
            await barrier.wait()
            started = time.monotonic()
            claimed = await asyncio.wait_for(
                queue.claim([JobKind.ENRICH], limit=limit), CLAIM_TIMEOUT
            )
            return ClaimWindow(
                keys=tuple(job.key for job in claimed),
                started_at=started,
                finished_at=time.monotonic(),
            )

        return list(await asyncio.gather(*(claimer(s) for s in self._sessions[:claimers])))


@pytest_asyncio.fixture
async def claimers(postgres_url: str) -> AsyncIterator[_PostgresConcurrentClaims]:
    """Named `claimers`, not `concurrent_claims`, so the class fixture below
    can request it. `JobQueueContract` defines `concurrent_claims` as a
    class-level fixture returning `None`; a class-level fixture shadows a
    module-level one of the same name, so a module fixture called
    `concurrent_claims` is simply never reached and the contract case skips
    itself with `requires_concurrency = True` set. Found by running it.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    sessions = [factory() for _ in range(2)]
    try:
        yield _PostgresConcurrentClaims(factory, sessions)
    finally:
        for one in sessions:
            await one.close()
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM jobs"))
            await cleanup.commit()
        await engine.dispose()


class TestPostgresJobQueue(JobQueueContract):
    """Every case in `JobQueueContract`, against real Postgres -- including
    the concurrency case the fake skips."""

    requires_concurrency = True

    @pytest.fixture
    def concurrent_claims(self, claimers: _PostgresConcurrentClaims) -> _PostgresConcurrentClaims:
        return claimers


async def test_a_second_worker_is_not_blocked_by_the_first_workers_claim(
    claimers: _PostgresConcurrentClaims,
) -> None:
    """`SKIP LOCKED`, isolated from everything else.

    The first worker claims the only job and does not commit, so its row lock
    stands. The second worker must come back empty-handed *promptly*. With a
    bare `FOR UPDATE` it waits on that lock instead of skipping past it -- a
    pool of workers degenerates into a queue of one -- and with no locking at
    all its `UPDATE` waits on the same row. Both spellings reach the timeout;
    only `SKIP LOCKED` answers.

    This is the single case that would still fail if `SKIP LOCKED` were
    deleted and everything else left alone. `test_two_workers_never_claim_the
    _same_job` would too, but through a timeout inside `asyncio.gather`, which
    is a noisier signal than this one.
    """
    windows = await claimers.run(keys=["only"], claimers=1)
    assert windows[0].keys == ("only",)

    second = claimers.session(1)
    queue = PostgresJobQueue(second, max_attempts=5, backoff_seconds=1.0)
    assert await asyncio.wait_for(queue.claim([JobKind.ENRICH]), CLAIM_TIMEOUT) == []


async def test_two_workers_split_two_jobs_rather_than_queueing_behind_each_other(
    claimers: _PostgresConcurrentClaims,
) -> None:
    """The property `SKIP LOCKED` exists for, stated positively.

    Two runnable jobs and two overlapping claimers: each takes one. A bare
    `FOR UPDATE` makes the second wait for the first's transaction to end
    before it can even look at the second row, which at a 1,126,674-job
    backfill is the difference between a worker pool and a worker.
    """
    windows = await claimers.run(keys=["a", "b"], claimers=2)
    claimed = sorted(key for window in windows for key in window.keys)
    assert claimed == ["a", "b"]
    assert all(len(window.keys) == 1 for window in windows), [w.keys for w in windows]


async def test_the_claim_query_uses_the_partial_index(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    """`ix_jobs_claim` is `(priority DESC, created_at) WHERE status =
    'pending'`. A claim that sorts instead of scanning it is a sort over the
    whole queue on every single claim, and at a 1.1M-item backfill that is the
    difference between a worker and a bottleneck.

    Explains the repository's **own** statement, binds and all, rather than a
    hand-copied lookalike -- the copy drifts, and a plan assertion about a
    query nothing runs is worse than none.

    **Scoped to the `claimable` CTE deliberately, and here is the measurement
    that says why.** The claim is two stages: a `LIMIT`ed, locking select
    (`claimable`) and an `UPDATE ... FROM` it. Only the first has an ordering
    to serve and only the first grows with queue depth. Explaining the whole
    statement at 2,000 / 50,000 / 300,000 pending rows (`pgvector/pgvector:
    pg17`, 2026-07-31) shows the selection stage on
    `Index Scan using ix_jobs_claim` at every size, and the *update* stage
    switching from `Hash Join` over a `Seq Scan` (2,000 rows, where a seq scan
    genuinely is cheaper -- cost 45) to `Nested Loop` + `Index Scan using
    pk_jobs` from 50,000 rows up. So an unscoped "no Seq Scan anywhere"
    assertion fails on a small fixture for a plan that is correct, and passes
    at scale for the wrong reason. This asserts the property that is actually
    load-bearing.
    """
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.MATCH, key=f"m-{index}", priority=JobPriority.NEW)
            for index in range(2_000)
        ]
    )
    await session.execute(text("ANALYZE jobs"))
    plan = "\n".join(
        (
            await session.execute(
                text("EXPLAIN " + PostgresJobQueue.claim_sql()),
                {"kinds": [JobKind.MATCH.value], "limit": 20},
            )
        )
        .scalars()
        .all()
    )
    selection = plan.split("CTE claimable", 1)[1].split("CTE claimed", 1)[0]
    assert "Index Scan using ix_jobs_claim" in selection, plan
    assert "Seq Scan" not in selection, plan
    assert "Sort" not in selection, "the index is supposed to be the ordering, not a sort over it"


async def test_backoff_is_jittered(session: AsyncSession) -> None:
    """A fixed backoff makes every job that failed in the same batch retry at
    the same instant -- a thundering herd against the upstream that was
    already struggling. The fake's schedule is unjittered and cannot catch
    this; only the real one is asserted on."""
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=60.0)
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.ENRICH, key=f"t-{index}", priority=JobPriority.NEW)
            for index in range(20)
        ]
    )
    claimed = await queue.claim([JobKind.ENRICH], limit=20)
    assert len(claimed) == 20
    instants = set()
    for job in claimed:
        failed = await queue.fail(job.id, error="upstream said no", retryable=True)
        assert failed is not None and failed.run_after is not None
        instants.add(failed.run_after)
    assert len(instants) > 1, "every retry landed on the same instant"


async def test_the_backoff_never_draws_zero(session: AsyncSession) -> None:
    """Equal jitter, not full jitter: the delay is a uniform draw from
    `[base/2, base)` rather than from `[0, base)`.

    Full jitter is the shape the plan proposed and the one AWS's article
    names, and it is wrong for this queue: its minimum draw is arbitrarily
    close to zero, so some fraction of failures against a broken upstream
    retry *immediately* -- the hot loop the backoff exists to prevent, just
    for fewer jobs. A guaranteed floor costs nothing (the spread is what
    breaks the herd, not the reachability of zero) and makes "a failed job is
    not immediately re-claimable" a property rather than a probability.
    """
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=10.0)
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.ENRICH, key=f"t-{index}", priority=JobPriority.NEW)
            for index in range(50)
        ]
    )
    floor = (
        await session.execute(text("SELECT clock_timestamp() + make_interval(secs => 5) AS floor"))
    ).scalar_one()
    for job in await queue.claim([JobKind.ENRICH], limit=50):
        failed = await queue.fail(job.id, error="upstream said no", retryable=True)
        assert failed is not None and failed.run_after is not None
        assert failed.run_after >= floor, f"{failed.run_after} is less than half a base interval"


async def test_the_backoff_grows_with_the_attempt_count(session: AsyncSession) -> None:
    """Exponential, not flat. A flat backoff retries a wedged upstream at a
    constant rate forever, which is the same hot loop one order of magnitude
    down."""
    queue = PostgresJobQueue(session, max_attempts=9, backoff_seconds=10.0)
    await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
    delays = []
    for _ in range(4):
        await session.execute(text("UPDATE jobs SET run_after = NULL"))
        claimed = await queue.claim([JobKind.ENRICH])
        before = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
        failed = await queue.fail(claimed[0].id, error="nope", retryable=True)
        assert failed is not None and failed.run_after is not None
        delays.append((failed.run_after - before).total_seconds())
    # Each band is [5 * 2^n, 10 * 2^n); consecutive bands do not overlap, so
    # the ordering is a property rather than a lucky draw.
    assert delays[0] < delays[1] < delays[2] < delays[3], delays


async def test_an_empty_key_is_a_port_error_not_an_integrity_error(
    queue: PostgresJobQueue,
) -> None:
    """`ck_jobs_key_not_empty` reaching the caller as a raw `IntegrityError`
    would make `services/` import `sqlalchemy.exc` to handle it, which is the
    thing ADR-0009 forbids. The staging table carries no constraints, so this
    fires one statement later, at the `INSERT ... SELECT`, which is why
    catching `IntegrityError` is sufficient."""
    with pytest.raises(RepositoryConflict) as caught:
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="", priority=JobPriority.NEW)])
    assert caught.value.constraint == "ck_jobs_key_not_empty"


async def test_an_out_of_range_priority_is_a_port_error(queue: PostgresJobQueue) -> None:
    """`JobRequest` is a plain frozen dataclass, not a `DomainModel`, so
    nothing validates `priority` before it reaches the column. That is
    deliberate -- promotion is `GREATEST` in SQL over an `int` -- and it means
    `ck_jobs_priority_range` is the only thing standing between a caller's
    typo and a row nothing can ever claim in the right order."""
    with pytest.raises(RepositoryConflict) as caught:
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="m1", priority=1_000)])
    assert caught.value.constraint == "ck_jobs_priority_range"


async def test_a_caught_conflict_leaves_the_session_usable(queue: PostgresJobQueue) -> None:
    """Postgres aborts the entire transaction on any statement error until a
    ROLLBACK, so without a SAVEPOINT a caught conflict poisons the session for
    the caller's next, unrelated call -- and this queue's caller commits a
    batch of enqueues together with the walk's own sync-run checkpoint."""
    with pytest.raises(RepositoryConflict):
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="", priority=JobPriority.NEW)])
    assert await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="m1", priority=50)]) == 1


async def test_a_failed_batch_writes_none_of_itself(queue: PostgresJobQueue) -> None:
    """The SAVEPOINT is what makes a batch atomic across its staging DDL, its
    `COPY`, and its upsert. Half of a 1,000-job enqueue landing would leave a
    walk unable to tell what it still owes."""
    with pytest.raises(RepositoryConflict):
        await queue.enqueue(
            [
                JobRequest(kind=JobKind.MATCH, key="good", priority=JobPriority.NEW),
                JobRequest(kind=JobKind.MATCH, key="", priority=JobPriority.NEW),
            ]
        )
    assert await queue.depth() == dict.fromkeys(JobKind, 0)


async def test_a_duplicate_inside_one_batch_would_raise_without_distinct_on(
    queue: PostgresJobQueue,
) -> None:
    """The `CardinalityViolationError` trap, named rather than merely
    survived. `test_a_duplicate_inside_one_batch_is_tolerated` passes in the
    fake because a dict cannot hold a key twice; here it is a real
    `ON CONFLICT DO UPDATE command cannot affect row a second time` that only
    `SELECT DISTINCT ON (kind, key)` avoids. Asserted at 1,000 duplicates
    because a walk really does re-yield pages."""
    request = JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)
    assert await queue.enqueue([request] * 1_000) == 1
    assert (await queue.depth())[JobKind.MATCH] == 1


async def test_the_claim_returns_the_row_as_stored(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    """`Job` and `jobs` are in exact 1:1 column correspondence, and
    `extra="forbid"` means a column this port forgot to map is a
    `ValidationError` rather than a silent drop. Asserted through a claim
    because `RETURNING jobs.*` is the one place the whole row round-trips."""
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.ENRICH,
                key="t1",
                priority=JobPriority.VISIBLE,
                traceparent="00-d14524c7eba73194c64d589cdd69488a-770641a119523a53-01",
            )
        ]
    )
    claimed = (await queue.claim([JobKind.ENRICH]))[0]
    stored = (
        (await session.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": claimed.id}))
        .mappings()
        .one()
    )
    assert claimed.kind is JobKind.ENRICH
    assert claimed.status is JobStatus.RUNNING
    assert claimed.priority == int(JobPriority.VISIBLE)
    assert claimed.created_at == stored["created_at"]
    assert claimed.updated_at == stored["updated_at"]


async def test_requeue_running_spares_a_claim_younger_than_the_cutoff(
    queue: PostgresJobQueue,
) -> None:
    """`older_than_seconds` exists so a future multi-worker deployment can
    recover a dead worker's claims without stealing a live worker's. A
    requeue that ignored it would hand every in-flight job to whoever
    restarted last."""
    await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
    await queue.claim([JobKind.ENRICH])
    assert await queue.requeue_running(older_than_seconds=3_600) == 0
    assert await queue.requeue_running() == 1


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    seen: list[str] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def test_an_enqueue_costs_the_same_number_of_statements_however_big_it_is(
    queue: PostgresJobQueue, statement_counter: list[str]
) -> None:
    """A first full walk enqueues 1,126,674 match jobs. A per-row insert is
    the same design defect one port over, and the staged `COPY` is what makes
    the batch size a memory question rather than a round-trip one."""
    statement_counter.clear()
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.MATCH, key=f"small-{index}", priority=JobPriority.NEW)
            for index in range(5)
        ]
    )
    small = len(statement_counter)

    statement_counter.clear()
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.MATCH, key=f"large-{index}", priority=JobPriority.NEW)
            for index in range(2_000)
        ]
    )
    large = len(statement_counter)

    assert small == large, f"{small} statements for 5 jobs, {large} for 2,000"


async def test_completing_a_job_really_deletes_the_row(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    """The contract asserts this through `requeue_running`, which is as far
    as a port can see. Here the table itself is the witness -- PRD 10's
    `usher.jobs.queued` gauge is a count over this table, and a status change
    masquerading as a delete makes it grow by one row per title forever."""
    await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
    claimed = await queue.claim([JobKind.ENRICH])
    await queue.complete(claimed[0].id)
    assert (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0


async def test_completing_an_unknown_id_does_not_disturb_the_queue(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
    await queue.complete(uuid.uuid4())
    assert (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 1


async def test_the_claim_ordering_survives_a_planner_that_ignores_the_index(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    """The `created_at` key in the claim's `ORDER BY` is *redundant given*
    `ix_jobs_claim`, which already carries it -- so deleting it changes
    nothing any ordinary case can observe. Measured, not assumed: the
    mutation that drops it survives all 50 other cases in this file, because
    every one of them gets its ordering from the index scan rather than from
    the clause.

    It stops being redundant the moment the planner does not use that index,
    which is what this forces. Two things make the distinction observable at
    all, and both are necessary:

    - **The re-enqueue in the middle**, which is what makes heap order and age
      order disagree. An `UPDATE` writes a new tuple version further down the
      page while `created_at` stays put, so a seq scan reaches `new` before
      the re-written `old`. Without it, heap order *is* insertion order *is*
      age order and no ordering bug can show.
    - **`limit=1`, not `limit=2`.** The key under test is inside the
      `claimable` CTE, where it decides *which* rows the `LIMIT` keeps -- not
      the order they come back in, which the outer `ORDER BY` fixes anyway. A
      limit large enough to take every candidate selects the same set either
      way.
    """
    for key in ("old", "new", "old"):
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key=key, priority=JobPriority.NEW)])
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    claimed = await queue.claim([JobKind.ENRICH], limit=1)
    assert [job.key for job in claimed] == ["old"], "the oldest job at a priority goes first"


async def test_a_claim_is_ordered_even_when_its_update_stage_hash_joins(
    session: AsyncSession, queue: PostgresJobQueue
) -> None:
    """`UPDATE ... RETURNING` makes no promise about row order, and at 2,000
    rows the claim's second stage really is a `Hash Join` over a `Seq Scan` of
    `jobs` (measured -- see `test_the_claim_query_uses_the_partial_index`), so
    `RETURNING` hands rows back in heap order rather than in the order the
    `claimable` CTE selected them. The outer `ORDER BY` over the
    data-modifying CTE is what makes the port's documented ordering true
    rather than incidental.
    """
    await queue.enqueue(
        [
            JobRequest(kind=JobKind.ENRICH, key=f"bulk-{index}", priority=JobPriority.NEW)
            for index in range(2_000)
        ]
    )
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key="urgent", priority=JobPriority.DEMAND)]
    )
    await session.execute(text("ANALYZE jobs"))
    claimed = await queue.claim([JobKind.ENRICH], limit=20)
    assert claimed[0].key == "urgent", [job.key for job in claimed[:3]]
    keys = [(-job.priority, job.created_at) for job in claimed]
    assert keys == sorted(keys), "the claim came back out of order"
