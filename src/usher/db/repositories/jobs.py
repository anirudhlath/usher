"""The priority work queue, on `SELECT ... FOR UPDATE SKIP LOCKED`.

Implements `JobQueue` (`usher.ports.jobs`). Four set-based statements, one
per port method that writes, and this is the one module in the project where
row-level locking appears.

**Why `SKIP LOCKED` and not `FOR UPDATE`.** Both stop two workers running the
same job, and only one of them keeps a worker pool a pool. Without `SKIP
LOCKED` the second worker *blocks* on the first's uncommitted row lock until
that transaction ends -- so N workers process jobs strictly one at a time,
serialised behind whoever is slowest, and the failure looks like a
performance problem rather than a correctness one. Without `FOR UPDATE` at
all, both workers' `SELECT`s see the same pending row and the second's
`UPDATE` blocks on the first's row lock anyway; the queue still serialises,
it just does so one statement later. Pinned by
`tests/integration/test_job_queue.py`, whose claims are bounded by
`asyncio.wait_for` precisely because the wrong spellings hang rather than
answer.

**A park is one statement.** `_FAIL` decides "back off or park" inside the
`UPDATE` itself, in a `CASE` over the row's own `attempts`. The obvious
alternative -- read the row, compare in Python, write the outcome -- is two
round trips with the job unowned in between, so a process that dies between
them leaves a job that is neither running, nor pending, nor parked, and the
attempt ceiling PRD 08 rests on is enforced against a value that may already
be stale.

**`clock_timestamp()`, never `now()`.** `now()` is `transaction_timestamp()`
-- frozen for the life of the transaction. Every statement here is about the
instant it actually runs: a job that failed twenty minutes into a long
transaction must back off from *now*, not from when that transaction opened,
and `requeue_running`'s `updated_at <= clock_timestamp() - interval` cannot
match a claim made in the same transaction if the claim stamped a frozen
`now()` and the requeue compares against the same frozen value. Verified
directly against the integration suite, whose per-test fixture is one long
transaction and is therefore the shape that shows the difference.

**Equal jitter, not full jitter.** The delay is a uniform draw from
`[base/2, base) * 2^attempts`, not from `[0, base) * 2^attempts`. Full jitter
is the more commonly cited shape and it is wrong for this queue: its minimum
draw is arbitrarily close to zero, so some share of failures against a broken
upstream retry immediately -- the hot loop the backoff exists to prevent,
merely rationed. The spread is what breaks a thundering herd, and a
half-interval floor keeps all of it while making "a failed job is not
instantly re-claimable" a property rather than a probability.

`ck_jobs_key_not_empty` and `ck_jobs_priority_range` fire at the
`INSERT ... SELECT`, not during the `COPY`: `usher.db.staging`'s staging
tables are deliberately unconstrained, so a violation reaches SQLAlchemy and
is translatable. See that module's docstring before adding a constraint to
`_STAGING_DDL`.
"""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.ids import new_id
from usher.domain.jobs import Job, JobKind
from usher.ports.errors import RepositoryConflict
from usher.ports.jobs import JobQueue, JobRequest

_STAGING_DDL = """
CREATE TEMP TABLE stg_jobs (
    id uuid, kind text, key text, priority integer, traceparent text
) ON COMMIT DROP
"""

_COLUMNS = ("id", "kind", "key", "priority", "traceparent")

# `SELECT DISTINCT ON (kind, key)` is required rather than defensive: one
# statement may not hit the same conflict target twice (Postgres answers
# `CardinalityViolationError`), and a walk really does yield the same item in
# two pages. `ORDER BY ..., priority DESC` makes the survivor the highest
# priority in the batch, because promote-never-demote has to hold within a
# batch for the same reason it holds across batches.
_ENQUEUE = """
INSERT INTO jobs (id, kind, key, priority, status, traceparent, created_at, updated_at)
SELECT DISTINCT ON (kind, key)
       id, kind, key, priority, 'pending', traceparent,
       clock_timestamp(), clock_timestamp()
FROM stg_jobs
ORDER BY kind, key, priority DESC
ON CONFLICT (kind, key) DO UPDATE SET
    -- GREATEST, never `excluded.priority`: re-enqueueing at a lower priority
    -- must not demote. A background backfill sweep runs over the whole
    -- catalog and would otherwise demote every job a client is waiting on,
    -- which is the exact inverse of what PRD 03's demand promotion is for.
    priority = GREATEST(jobs.priority, excluded.priority),
    traceparent = COALESCE(excluded.traceparent, jobs.traceparent),
    -- `created_at` is deliberately absent from this SET clause. It is the
    -- starvation guard -- `ORDER BY priority DESC, created_at` -- so
    -- refreshing it on every nightly walk would push a job that keeps being
    -- re-seen behind everything enqueued since, forever, while it stayed
    -- perfectly claimable the whole time.
    updated_at = clock_timestamp()
-- Two conditions, and the second is a scale fix rather than a tidiness one.
--
-- Parked work is not un-parked by asking for it again, and is not counted as
-- written either. This clause does not change `status` in either direction
-- (nothing here does), so what it actually buys is that a parked job's
-- priority is not silently promoted while a human is looking at it, and that
-- `enqueue`'s return value stays "rows written".
--
-- `jobs.priority < excluded.priority` is what stops a nightly walk rewriting
-- the whole queue for no state change. Every batch of a walk re-enqueues a
-- job for every item it saw, and without this clause `ON CONFLICT DO UPDATE`
-- fires for each one: a new row version per job per night -- up to 1,126,674
-- of them at the one measured deployment -- on a table whose entire purpose
-- is to stay small, plus the WAL and the vacuum to match. Nothing observable
-- changes: `priority` is already `GREATEST(...)` of itself, `created_at` is
-- deliberately untouched (see below), and `updated_at` on a job nobody
-- claimed means nothing to anybody. With the clause, a re-seen job costs one
-- index probe and zero writes, and `enqueue` reports 0 rows written, which
-- is the honest number.
--
-- `GREATEST` is kept in the SET clause even though this predicate already
-- guarantees `excluded.priority` is the larger: the two say different
-- things (one is "when to write", the other "what to write"), and a future
-- edit to either must not silently depend on the other.
--
-- The cost is that a re-enqueue carrying a *new* `traceparent` at the same
-- priority no longer repoints the link -- which is the right answer anyway.
-- A background walk's trace is not the trace anyone wants a link to; a
-- demand promotion (M5) raises the priority and therefore does write.
WHERE jobs.status <> 'parked' AND jobs.priority < excluded.priority
"""

# The claimed rows come back ordered by the same key the CTE selected them
# with. `UPDATE ... RETURNING` has no `ORDER BY` of its own and makes no
# promise about row order, so the ordering the port documents is applied in
# an outer `SELECT` over the data-modifying CTE rather than assumed.
_CLAIM = """
WITH claimable AS (
    SELECT id FROM jobs
    WHERE status = 'pending'
      AND kind = ANY(:kinds)
      AND (run_after IS NULL OR run_after <= clock_timestamp())
    ORDER BY priority DESC, created_at
    LIMIT :limit
    -- SKIP LOCKED, not bare FOR UPDATE: without it a second worker blocks on
    -- the first worker's rows instead of moving past them, which turns a
    -- pool into a queue of one. Without FOR UPDATE at all, both workers read
    -- the same row and the second one blocks on the first's UPDATE instead.
    FOR UPDATE SKIP LOCKED
), claimed AS (
    UPDATE jobs SET status = 'running', updated_at = clock_timestamp()
    FROM claimable WHERE jobs.id = claimable.id
    RETURNING jobs.*
)
SELECT * FROM claimed ORDER BY priority DESC, created_at
"""

_FAIL = """
UPDATE jobs SET
    attempts = attempts + 1,
    last_error = :error,
    status = CASE
        -- `PortDataMalformed` in queue form: the upstream answered and the
        -- answer was wrong, so five identical retries only delay a human
        -- seeing it by the whole backoff schedule.
        WHEN NOT CAST(:retryable AS boolean) THEN 'parked'
        WHEN attempts + 1 >= :max_attempts THEN 'parked'
        ELSE 'pending'
    END,
    run_after = CASE
        WHEN NOT CAST(:retryable AS boolean) OR attempts + 1 >= :max_attempts THEN NULL
        -- Exponential with equal jitter: a uniform draw from
        -- [base/2, base) * 2^attempts. `random()` is evaluated per row by
        -- Postgres, so a batch of twenty failures against the same upstream
        -- gets twenty different instants instead of one thundering herd.
        -- `attempts` here is the pre-increment value, so the first retry
        -- waits one base interval rather than two.
        ELSE clock_timestamp() + make_interval(
            secs => :backoff_seconds * power(2, attempts) * (0.5 + random() / 2)
        )
    END,
    updated_at = clock_timestamp()
WHERE id = :id
RETURNING *
"""

# `status = 'running'` is the whole predicate that keeps this off parked
# poison: a requeue keyed on anything looser un-parks it on every restart,
# which is the failure parking exists to end, arriving through the recovery
# path. `attempts` and `last_error` are deliberately untouched -- a job that
# keeps killing its worker must still reach the ceiling.
_REQUEUE = """
UPDATE jobs SET status = 'pending', updated_at = clock_timestamp()
WHERE status = 'running'
  AND updated_at <= clock_timestamp() - make_interval(secs => :older_than_seconds)
"""

_DEPTH = "SELECT kind, count(*) AS depth FROM jobs WHERE status = 'pending' GROUP BY kind"

_PARKED = (
    "SELECT * FROM jobs WHERE status = 'parked' ORDER BY updated_at DESC, id DESC LIMIT :limit"
)


class PostgresJobQueue(JobQueue):
    """`max_attempts` and `backoff_seconds` are constructor arguments rather
    than reads of `Settings`: `db/` must not import `config` for the same
    reason `services/` must not, and both composition roots already pass every
    other tunable this way."""

    def __init__(self, session: AsyncSession, *, max_attempts: int, backoff_seconds: float) -> None:
        self._session = session
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    @staticmethod
    def claim_sql() -> str:
        """The literal claim statement, for `EXPLAIN` in the integration
        suite.

        A plan assertion against a hand-copied lookalike drifts from the
        statement that actually runs, and then asserts about a query nothing
        issues -- which is worse than not asserting, because it reads like
        coverage. Exposed as a method rather than by importing the private
        constant so the coupling is visible from this side too.
        """
        return _CLAIM

    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        if not requests:
            return 0
        try:
            # A SAVEPOINT for the same reason PostgresMediaItemRepository has
            # one: this port's caller genuinely has other pending work on the
            # session -- IngestService commits a batch of jobs together with
            # the walk's own sync-run checkpoint -- so a caught conflict must
            # not leave the session raising PendingRollbackError on the next
            # unrelated call. It also makes the batch atomic across the
            # staging DDL, the COPY and the upsert.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_STAGING_DDL,
                        table="stg_jobs",
                        columns=_COLUMNS,
                        records=[
                            (
                                new_id(),
                                # `.value`, not the member: asyncpg's binary
                                # COPY encodes what it is given, and every
                                # other staged write in this project spells
                                # the enum the same way.
                                request.kind.value,
                                request.key,
                                request.priority,
                                request.traceparent,
                            )
                            for request in requests
                        ],
                    )
                    result = cast(CursorResult[Any], await self._session.execute(text(_ENQUEUE)))
                    written = result.rowcount
        except IntegrityError as exc:
            raise RepositoryConflict(
                "a job batch conflicts with the queue's constraints",
                constraint=constraint_name(exc),
            ) from exc
        return written

    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        if not kinds or limit <= 0:
            return []
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_CLAIM),
                        {"kinds": [kind.value for kind in kinds], "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [Job.model_validate(dict(row)) for row in rows]

    async def complete(self, job_id: uuid.UUID) -> None:
        with self._session.no_autoflush:
            await self._session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})

    async def fail(self, job_id: uuid.UUID, *, error: str, retryable: bool) -> Job | None:
        with self._session.no_autoflush:
            row = (
                (
                    await self._session.execute(
                        text(_FAIL),
                        {
                            "id": job_id,
                            "error": error,
                            "retryable": retryable,
                            "max_attempts": self._max_attempts,
                            "backoff_seconds": self._backoff_seconds,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else Job.model_validate(dict(row))

    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        with self._session.no_autoflush:
            result = cast(
                CursorResult[Any],
                await self._session.execute(
                    text(_REQUEUE), {"older_than_seconds": older_than_seconds}
                ),
            )
        return result.rowcount

    async def depth(self) -> dict[JobKind, int]:
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_DEPTH))).all()
        # A GROUP BY returns only non-empty kinds, and a gauge that stops
        # reporting a series is indistinguishable from one reporting zero.
        counts = dict.fromkeys(JobKind, 0)
        counts.update({JobKind(row.kind): int(row.depth) for row in rows})
        return counts

    async def parked(self, *, limit: int = 100) -> list[Job]:
        if limit <= 0:
            return []
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_PARKED), {"limit": limit})).mappings().all()
        return [Job.model_validate(dict(row)) for row in rows]
