"""The priority work queue (PRD 03's read-through queue, PRD 08's job
reliability rules).

A driven port like every other, so `services/` depends on this ABC and never
on `usher.db` (ADR-0009). `usher.db.repositories.jobs.PostgresJobQueue` is
the concrete implementation, and it is the one place `SELECT ... FOR UPDATE
SKIP LOCKED` appears.

Same session/transaction ownership as the repository ports: every method
flushes, none commits. That matters more here than elsewhere -- a worker
claims a job, does its work, and completes it, and the claim must be
committed before the work starts or a second worker sees an unclaimed job
while the first is running it. `JobWorker` commits between claim and work
for exactly that reason, and the port says so rather than leaving it to be
discovered.

**A claim is a lock held for the length of a transaction, not a lease with a
timestamp.** That is why `claim` has no duration argument and `requeue_running`
takes an age instead: a SQL implementation's exclusion comes from the row lock
`FOR UPDATE SKIP LOCKED` takes, which the database releases when the claiming
transaction ends however it ends. What survives a *committed* claim is the
`running` status, and that is what `requeue_running` cleans up after a process
dies between committing its claim and finishing its work.
"""

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
        """Add work, deduplicated on `(kind, key)`. Returns rows written.

        **Re-enqueueing existing work promotes it and never demotes it.** A
        second request for the same `(kind, key)` at a higher priority raises
        the stored priority; at a lower one it leaves it alone. That is what
        makes M5's demand promotion ("requesting an unenriched title promotes
        its job to the front of the queue") a priority update rather than a
        schema change, and it is what stops a background backfill sweep from
        demoting a job a client is waiting on.

        Re-enqueueing does **not** un-park a parked job. Poison that a human
        has not looked at is not fixed by asking for it again, and PRD 08's
        whole point is that parked work stays visible rather than
        recirculating. A parked row is therefore not counted in the return
        value either: nothing was written.

        Re-enqueueing does not reset `created_at`, so a job repeatedly seen
        by successive walks keeps its place in the age tiebreak rather than
        being pushed to the back of its priority band every night.

        A batch may contain the same `(kind, key)` twice -- a walk can see
        the same item twice -- so an implementation deduplicates rather than
        assuming. Within a batch the highest priority wins, for the same
        reason it wins across batches.
        """

    @abstractmethod
    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        """Take up to `limit` runnable jobs, marking them `RUNNING`.

        Runnable means `status = pending` and `run_after` is null or in the
        past. Ordered **`priority` descending, then `created_at` ascending**,
        and the returned list is in that order: higher priority first (PRD
        03's scale puts 100 at the top), oldest first within a priority so
        nothing starves.

        Two workers must never claim the same job. Against a SQL store that
        is `FOR UPDATE SKIP LOCKED`; `FOR UPDATE` alone serialises the
        workers instead of distributing them -- the second worker blocks on
        the first's uncommitted claim rather than moving past it -- and a
        plain `SELECT` followed by an `UPDATE` hands the same row to both.

        The claim must be committed before the work starts -- see the module
        docstring.
        """

    @abstractmethod
    async def complete(self, job_id: uuid.UUID) -> None:
        """The work succeeded. **Deletes the row.**

        Not a status change: `JobStatus` has no `DONE` member, because the
        only two interesting populations are "waiting" and "poisoned" and a
        terminal row per title would make PRD 10's `usher.jobs.queued` gauge
        a count over a table that only grows. Redelivery is safe by
        construction (PRD 08), so losing the record of a success costs
        nothing.

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
        """The work raised. Back it off, or park it.

        `retryable=False` **parks immediately**, whatever the attempt count.
        That is `PortDataMalformed`'s contract in queue form: "the upstream
        answered, and the answer was wrong. Retrying does not help." Backing
        it off five times first is five identical failures and a five-times
        longer wait before a human sees it. `retry_after_seconds` is not
        consulted on this arm -- there is no backoff to floor.

        `retryable=True` increments `attempts` and sets `run_after` to an
        exponentially-backed-off, jittered instant -- **unless** `attempts`
        has reached the implementation's ceiling, in which case the job is
        parked with `error`. PRD 08: "after N attempts a job is *parked* with
        its error, not retried forever and not silently dropped."

        The backoff must be jittered rather than a fixed multiple of the
        attempt count. A whole batch of jobs that failed against the same
        upstream in the same second otherwise retries in the same second,
        which is a thundering herd against something already struggling.

        `retry_after_seconds` is a **floor added to** the jittered backoff,
        not a replacement for it: an upstream's own `Retry-After` (seconds, or
        an HTTP-date already resolved to seconds by the caller -- see
        `PortRateLimited`) must never be answered *sooner* than it asked, but
        a whole batch rate-limited by the same upstream in the same second
        must still not retry in the identical instant. `None` means the
        upstream gave no hint, and is equivalent to `0.0`. A hint that has
        already elapsed (a negative value, e.g. from an HTTP-date already in
        the past) must not pull the retry *earlier* than the ordinary
        schedule would have -- the job must still not be instantly
        re-claimable. Keyword-only with a default, so it does not change any
        existing call site.

        Returns the job as it now stands, so a caller can log or count the
        park, or `None` if the id is unknown (a worker whose claim was
        requeued out from under it by a restart).

        `error` is `str(exc)`, never the exception object and never a
        payload: PRD 08's credentials-never-logged rule applies to a column
        an operator reads.
        """

    @abstractmethod
    async def touch(self, job_ids: Sequence[uuid.UUID]) -> int:
        """Say these claims are still being worked on. Returns rows moved.

        The heartbeat half of the lease `requeue_running` reads. An
        implementation moves whatever `requeue_running` compares against -- for
        the SQL store that is `updated_at` -- and **only for rows still
        `running`**, so a job another worker already recovered, completed or
        parked is not resurrected by a beat that was already in flight.

        Idempotent, and silent about ids it does not find: a worker whose claim
        was recovered out from under it has nothing useful to do with the news
        and must not fail its own job over its own telemetry.

        `requeue_running`'s age threshold is meaningless without this. With no
        heartbeat the threshold has to exceed the longest job a deployment can
        run -- hours, for a `bootstrap` phase -- so the orphan window becomes
        hours; with one, the threshold is about the *process* still being
        alive and can be minutes.
        """

    @abstractmethod
    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        """Return claimed-but-unfinished jobs to `pending`. Returns how many.

        PRD 08: "Startup requeues anything left `in_progress` by an unclean
        shutdown."

        ⚠️ **The `0.0` default requeues everything currently `RUNNING`, which
        is safe at exactly one worker with exactly one job in flight -- and
        that deployment no longer exists.** `JobWorker` runs its jobs
        concurrently, so at `0.0` a worker recovering orphans would steal its
        *own* live claims; two processes would steal each other's. M9's S3
        measured the consequence of having only this lever: one of three
        workers died holding 20 claims, and the only way to recover them also
        corrupted the other two, so they were written off. `JobWorker.recover`
        therefore always passes an explicit `older_than_seconds`, paired with
        `touch` above.

        The default is kept at `0.0` rather than moved, because a default that
        silently *skips* work would be the worse failure of the two: a caller
        who meant "everything" and got "nothing older than five minutes" sees
        an empty return value and no error.

        Does not clear `attempts` or `last_error`: a job that has already
        failed twice and was then interrupted is still two attempts in, and
        an unclean shutdown that keeps happening on the same job must reach
        the attempt ceiling rather than looping forever at zero.
        """

    @abstractmethod
    async def depth(self) -> dict[JobKind, int]:
        """Pending count per kind, for PRD 10's `usher.jobs.queued` gauge.

        Always returns every `JobKind` as a key, `0` for an empty one -- a
        `GROUP BY` returns only non-empty kinds, and a gauge that stops
        reporting a series is indistinguishable from one reporting zero.

        Counts `pending` only. A claimed job is work in progress rather than
        queue depth, and a parked one is not waiting for a worker at all --
        `parked()` is what surfaces those.
        """

    @abstractmethod
    async def parked(self, *, limit: int = 100) -> list[Job]:
        """Parked jobs, newest first. PRD 08: "Parked jobs are listed in the
        admin API and counted in metrics. Silent failure is the thing worth
        engineering against."
        """
