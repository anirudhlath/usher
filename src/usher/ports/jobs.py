"""The priority work queue: claim, heartbeat, complete, back off, park."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.jobs import Job, JobKind


@dataclass(frozen=True, slots=True)
class JobRequest:
    """Work to enqueue.

    Deliberately not a `Job`: a caller does not choose an id, a status, or an
    attempt count, and letting it hand over a whole `Job` would make "enqueue
    this as already-parked with nine attempts" a reachable state.
    """

    kind: JobKind
    key: str
    priority: int
    traceparent: str | None = None


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        """Add work, deduplicated on `(kind, key)`.

        Returns rows written.
        """

    @abstractmethod
    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        """Take up to `limit` runnable jobs, marking them `RUNNING`.

        Runnable means `status = pending` and `run_after` is null or in the
        past. The returned list is ordered `priority` descending, then
        `created_at` ascending -- higher priority first, oldest first within
        a priority so nothing starves.

        Two workers must never claim the same job. Against a SQL store that
        is `FOR UPDATE SKIP LOCKED`; `FOR UPDATE` alone serialises the
        workers instead of distributing them -- the second worker blocks on
        the first's uncommitted claim rather than moving past it -- and a
        plain `SELECT` followed by an `UPDATE` hands the same row to both.

        The claim must be committed before the work starts, or a crash loses
        the claim and the job runs twice.
        """

    @abstractmethod
    async def complete(self, job_id: uuid.UUID) -> None:
        """The work succeeded, and the row is deleted.

        Not a status change: `JobStatus` has no `DONE` member, because the
        only interesting populations are "waiting" and "poisoned", and a
        terminal row per job would make the `usher.jobs.queued` gauge a count
        over a table that only grows. Redelivery is safe by construction, so
        losing the record of a success costs nothing.

        Idempotent: an id that no longer exists is not an error, because a
        worker whose claim was requeued and re-completed by someone else has
        nothing useful to do with the news.
        """

    @abstractmethod
    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> Job | None:
        """The work raised.

        Back it off, or park it.
        """

    @abstractmethod
    async def touch(self, job_ids: Sequence[uuid.UUID]) -> int:
        """Say these claims are still being worked on.

        Returns rows moved.

        The heartbeat half of the lease `requeue_running` reads. It moves
        whatever `requeue_running` compares against, and only for rows still
        `running`, so a job another worker already recovered, completed or
        parked is not resurrected by a beat still in flight.

        Idempotent, and silent about ids it does not find: a worker whose
        claim was recovered out from under it must not fail its own job over
        its own telemetry.

        `requeue_running`'s age threshold is meaningless without this. With
        no heartbeat the threshold has to exceed the longest job a deployment
        can run, so the orphan window becomes hours; with one, the threshold
        is about the process still being alive and can be minutes.
        """

    @abstractmethod
    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        """Return claimed-but-unfinished jobs to `pending`.

        Returns how many.
        """

    @abstractmethod
    async def depth(self) -> dict[JobKind, int]:
        """Pending count per kind, for the `usher.jobs.queued` gauge.

        Always returns every `JobKind` as a key, `0` for an empty one -- a
        `GROUP BY` returns only non-empty kinds, and a gauge that stops
        reporting a series is indistinguishable from one reporting zero.

        Counts `pending` only. A claimed job is work in progress rather than
        queue depth, and a parked one is not waiting for a worker at all --
        `parked()` is what surfaces those.
        """

    @abstractmethod
    async def parked(self, *, limit: int = 100) -> list[Job]:
        """Parked jobs, newest first.

        Parked work is listed and counted rather than dropped: silent failure
        is the thing worth engineering against.
        """
