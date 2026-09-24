"""In-memory `JobQueue`."""

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

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> Job | None:
        found = self._find(job_id)
        if found is None:
            return None
        stored = self._jobs[found]
        attempts = stored.attempts + 1
        # `not retryable` parks whatever the count: PortDataMalformed means
        # the upstream answered and the answer was wrong, so five identical
        # retries only delay a human seeing it.
        parked = not retryable or attempts >= self._max_attempts
        # A floor, never a replacement -- see `PostgresJobQueue`'s `GREATEST`
        # and the module docstring's eighth divergence. A hint that already
        # elapsed (negative) must not pull the retry earlier than the
        # ordinary schedule, so it is clamped at zero, same as the SQL arm.
        hint = 0.0 if retry_after_seconds is None else max(retry_after_seconds, 0.0)
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
                else _now() + timedelta(seconds=hint + self._backoff_seconds * 2**stored.attempts)
            ),
            updated_at=_now(),
        )
        self._jobs[found] = updated
        return updated

    async def touch(self, job_ids: Sequence[uuid.UUID]) -> int:
        """`updated_at` forward, for running rows only.

        The same column `requeue_running` below compares against, which is what
        makes the pair a lease here as well as in SQL. A `running` filter for
        the same reason the statement has one: a beat that arrives after
        another worker recovered the job must not resurrect it.
        """
        wanted = set(job_ids)
        moved = 0
        for key, job in list(self._jobs.items()):
            if job.id in wanted and job.status is JobStatus.RUNNING:
                self._jobs[key] = job.evolve(updated_at=_now())
                moved += 1
        return moved

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

    def backdate(self, *, seconds: float) -> None:
        """Move every stored `updated_at` back, so a lease can be observed.

        Test-only, deliberately absent from the port, and for `clear_backoff`'s
        reason below: the alternative is a case that sleeps for the length of a
        lease, and a suite that waits five minutes to watch a threshold fire is
        a suite nobody runs. Backdating the *row* rather than advancing a clock
        keeps `_now()` a real reading, which is what the Postgres arm does too
        (`clock_timestamp()` is not injectable).
        """
        moved = timedelta(seconds=seconds)
        for key, job in list(self._jobs.items()):
            self._jobs[key] = job.evolve(updated_at=job.updated_at - moved)

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
