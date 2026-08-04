"""In-memory `JobQueue`.

**Where this is more forgiving than Postgres, on purpose.** Six places, and
the first is not a nuance -- it is the whole point of the port:

- **Nothing here can express `SELECT ... FOR UPDATE SKIP LOCKED`.** This is
  one dict behind one event loop; there is no second session, no row lock,
  and therefore no way for two claimers to contend at all.
  `test_two_workers_never_claim_the_same_job` is **skipped** for this class
  rather than passed, because a pass would be a claim about locking that
  this file cannot make. `test_a_claimed_job_is_not_claimed_again` is the
  part that *is* expressible here, and it is a genuinely weaker property:
  it catches a claim that never wrote `status = running`, and nothing else.
  `tests/integration/test_job_queue.py` is where the locking is actually
  pinned, against two real Postgres backends with genuinely overlapping
  claims.
- **The backoff is deterministic, not jittered.** `fail` here computes
  exactly `backoff_seconds * 2 ** attempts`, so every job that failed in the
  same batch retries at the same instant and nothing in this run notices.
  A thundering herd against an upstream that was already struggling is
  invisible here and is pinned by
  `tests/integration/test_job_queue.py::test_backoff_is_jittered`.
- **No `(kind, key)` unique constraint** -- it is a dict key, so a duplicate
  is structurally impossible rather than rejected. The real one needs
  `SELECT DISTINCT ON (kind, key)` before its `ON CONFLICT` or Postgres
  raises `CardinalityViolationError`, and
  `test_a_duplicate_inside_one_batch_is_tolerated` passes here for a reason
  that has nothing to do with the code under test.
- **No CHECK constraints.** `ck_jobs_priority_range` and
  `ck_jobs_key_not_empty` are enforced here only by `Job`'s own pydantic
  bounds, which fire at a different moment and with a different exception
  type than Postgres's do.
- **No transaction**, so a batch that raises part-way cannot leave a session
  poisoned, and nothing here exercises the enqueue's SAVEPOINT.
- **`created_at` ties break on insertion order**, because `list.sort` is
  stable. Postgres makes no such promise for equal `(priority, created_at)`
  keys, so a claim ordering that looks total here is partial there. The
  contract's age case enqueues in separate calls specifically so the two
  orderings are comparable.
- **`enqueue` reports a no-op re-enqueue as a row written, and Postgres does
  not.** The update branch below adds one to its count whatever it changed;
  the real `_ENQUEUE`'s conflict clause carries
  `AND jobs.priority < excluded.priority`, so re-enqueueing work that is
  already at that priority matches nothing and answers **0** -- which M4
  added deliberately, to stop a nightly walk rewriting 1,126,674 unchanged
  rows. Anything whose behaviour turns on the *count* rather than on the
  stored row is therefore untestable here: `TitleReadService._promote`
  returns whether an enqueue was attempted, and the version that returned
  "a row changed" passes every case in `tests/unit/test_services_titles.py`
  and fails `tests/integration/test_services_titles.py`. Measured
  2026-08-01. Not fixed here, because the fake would then have to model the
  whole promotion predicate to stay honest about the parked and
  higher-priority branches too, and a fake that reimplements the statement
  is a second implementation rather than a stand-in.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from usher.domain.jobs import Job, JobKind, JobStatus
from usher.ports.jobs import JobQueue, JobRequest

_Key = tuple[JobKind, str]


class FakeJobQueue(JobQueue):
    def __init__(self, *, max_attempts: int = 5, backoff_seconds: float = 1.0) -> None:
        self._jobs: dict[_Key, Job] = {}
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        # Highest priority wins within the batch, matching the real one's
        # `SELECT DISTINCT ON (kind, key) ... ORDER BY kind, key, priority
        # DESC`. Promote-never-demote has to hold inside a batch as well as
        # across batches: one walk can see the same item twice.
        deduped: dict[_Key, JobRequest] = {}
        for request in requests:
            key = (request.kind, request.key)
            current = deduped.get(key)
            if current is None or request.priority > current.priority:
                deduped[key] = request
        written = 0
        for key, request in deduped.items():
            stored = self._jobs.get(key)
            if stored is None:
                self._jobs[key] = Job(
                    kind=request.kind,
                    key=request.key,
                    priority=request.priority,
                    traceparent=request.traceparent,
                )
                written += 1
                continue
            # Poison a human has not looked at is not fixed by asking for it
            # again, and it is not counted as written either -- nothing was.
            if stored.status is JobStatus.PARKED:
                continue
            self._jobs[key] = stored.evolve(
                # `max`, never the incoming value: a background backfill
                # sweep must not demote a job a client is waiting on.
                priority=max(stored.priority, request.priority),
                traceparent=request.traceparent or stored.traceparent,
                updated_at=_now(),
            )
            written += 1
        return written

    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        wanted = set(kinds)
        now = _now()
        runnable = [
            job
            for job in self._jobs.values()
            if job.kind in wanted
            and job.status is JobStatus.PENDING
            and (job.run_after is None or job.run_after <= now)
        ]
        # `-priority` then `created_at`: highest priority first, oldest first
        # within a priority so nothing starves. Stable, so equal keys keep
        # insertion order -- see the module docstring; Postgres promises no
        # such thing.
        runnable.sort(key=lambda job: (-job.priority, job.created_at))
        claimed = []
        for job in runnable[: max(limit, 0)]:
            running = job.evolve(status=JobStatus.RUNNING, updated_at=now)
            self._jobs[(running.kind, running.key)] = running
            claimed.append(running)
        return claimed

    async def complete(self, job_id: uuid.UUID) -> None:
        found = self._find(job_id)
        if found is not None:
            del self._jobs[found]

    async def fail(self, job_id: uuid.UUID, *, error: str, retryable: bool) -> Job | None:
        found = self._find(job_id)
        if found is None:
            return None
        stored = self._jobs[found]
        attempts = stored.attempts + 1
        # `not retryable` parks whatever the count: PortDataMalformed means
        # the upstream answered and the answer was wrong, so five identical
        # retries only delay a human seeing it.
        parked = not retryable or attempts >= self._max_attempts
        updated = stored.evolve(
            attempts=attempts,
            last_error=error,
            status=JobStatus.PARKED if parked else JobStatus.PENDING,
            run_after=(
                None
                if parked
                # Deterministic, not jittered -- the divergence this fake's
                # docstring names. `stored.attempts` (pre-increment) is the
                # exponent, so the first retry waits one base interval.
                else _now() + timedelta(seconds=self._backoff_seconds * 2**stored.attempts)
            ),
            updated_at=_now(),
        )
        self._jobs[found] = updated
        return updated

    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        requeued = 0
        for key, job in list(self._jobs.items()):
            if job.status is JobStatus.RUNNING and job.updated_at <= cutoff:
                # `attempts` and `last_error` survive deliberately: a job that
                # keeps killing its worker must still reach the ceiling.
                self._jobs[key] = job.evolve(status=JobStatus.PENDING, updated_at=_now())
                requeued += 1
        return requeued

    async def depth(self) -> dict[JobKind, int]:
        counts = dict.fromkeys(JobKind, 0)
        for job in self._jobs.values():
            if job.status is JobStatus.PENDING:
                counts[job.kind] += 1
        return counts

    async def parked(self, *, limit: int = 100) -> list[Job]:
        found = [job for job in self._jobs.values() if job.status is JobStatus.PARKED]
        found.sort(key=lambda job: (job.updated_at, job.id), reverse=True)
        return found[: max(limit, 0)]

    async def clear_backoff(self) -> None:
        """Test-only hook, deliberately absent from the port.

        The contract needs to advance past a backoff without sleeping through
        it; nothing in `src/` would ever call this. The Postgres side spells
        the same thing `UPDATE jobs SET run_after = NULL`.
        """
        for key, job in list(self._jobs.items()):
            if job.run_after is not None:
                self._jobs[key] = job.evolve(run_after=None)

    def jobs_of(self, kind: JobKind) -> list[Job]:
        """Test-only hook, deliberately absent from the port.

        `depth()` answers how many, and a case asserting *which* row was
        written -- its key, its priority -- would otherwise reach into
        `_jobs`. Same status as `clear_backoff` above: nothing in `src/`
        would ever call it, and the real queue answers the same question with
        a `SELECT`.
        """
        return [job for job in self._jobs.values() if job.kind is kind]

    def _find(self, job_id: uuid.UUID) -> _Key | None:
        return next((key for key, job in self._jobs.items() if job.id == job_id), None)


def _now() -> datetime:
    return datetime.now(UTC)
