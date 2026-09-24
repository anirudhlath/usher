"""The queue's consumer (PRD 08's job-reliability rules)."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.trace import Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from usher.domain.jobs import BOOTSTRAP_CONCURRENCY, Job, JobKind
from usher.ports.errors import PortDataMalformed, PortRateLimited, UsherPortError
from usher.ports.jobs import JobQueue
from usher.services.events import DeferredEventPublisher

Handler = Callable[[Job], Awaitable[None]]

#: How long a claim may sit in `running` without being heartbeated before any
#: worker may take it back.
DEFAULT_LEASE_SECONDS: Final = 300.0

#: How much of the lease may pass between heartbeats. A third, so two
#: consecutive missed beats -- a stalled event loop, a slow database -- still
#: leave a margin before another worker may take the claim.
HEARTBEAT_FRACTION: Final = 3.0

#: Per-kind ceilings on jobs in flight, `None` meaning "whatever the deployment
#: configured globally" (`Settings.job_concurrency`).
KIND_CONCURRENCY: Final[Mapping[JobKind, int | None]] = MappingProxyType(
    {
        JobKind.ENRICH: None,
        JobKind.MATCH: 4,
        JobKind.WATCH_HISTORY: 4,
        JobKind.WATCH_WRITEBACK: 4,
        JobKind.DERIVE: 4,
        JobKind.INDEX: 1,
        JobKind.CURATE: 1,
        JobKind.SYNC: 1,
        JobKind.BOOTSTRAP: BOOTSTRAP_CONCURRENCY,
    }
)

_tracer = trace.get_tracer("usher.jobs")
_meter = metrics.get_meter("usher.jobs")
_job_duration = _meter.create_histogram(
    "usher.jobs.duration", unit="s", description="Wall time per job"
)
_propagator = TraceContextTextMapPropagator()


@dataclass(frozen=True, slots=True)
class JobScope:
    """One unit of work's own session, expressed without naming a session.

    `queue` and `commit` are that session's; `handlers` are the ones its
    repositories are bound to; `events` is the buffer *its* handlers publish
    into and the one `JobWorker` flushes or discards for that job alone.

    A dataclass rather than four arguments because the four are one thing: a
    scope whose commit belonged to a different session from its queue is the
    exact defect this type exists to make unspellable.
    """

    queue: JobQueue
    commit: Callable[[], Awaitable[None]]
    handlers: Mapping[JobKind, Handler]
    events: DeferredEventPublisher


#: Opens one. Spelled as a callable returning a context manager rather than as
#: a session factory so `usher.services` never imports SQLAlchemy and a test can
#: supply one over fakes -- the shape `usher.composition.UnitOfWork` already
#: has, one layer up.
JobScopes = Callable[[], AbstractAsyncContextManager[JobScope]]


class JobWorker:
    def __init__(
        self,
        scopes: JobScopes,
        concurrency: Mapping[JobKind, int],
        *,
        max_in_flight: int,
        batch_size: int = 20,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._scopes = scopes
        # The registration list and the concurrency table are one object:
        # `run_once` claims `list(self._concurrency)`, so a kind this worker cannot
        # run cannot be claimed, and a kind it can run cannot be missing a ceiling.
        self._concurrency = dict(concurrency)
        self._max_in_flight = max(1, max_in_flight)
        # Refill when the pool is half empty rather than when it is empty: a
        # claim per freed slot is a round trip per job, and a claim only when
        # the last job finishes is the straggler stall this replaced.
        self._low_water = self._max_in_flight // 2
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._gates = {
            kind: asyncio.Semaphore(min(limit, self._max_in_flight))
            for kind, limit in self._concurrency.items()
        }
        self._in_flight: set[uuid.UUID] = set()

    @property
    def registered_kinds(self) -> frozenset[JobKind]:
        """Exactly what `run_once` will claim.

        A read-only view rather than a test reaching into `_concurrency`. Four
        of the nine kinds are registered conditionally by
        `composition.build_worker` -- `ENRICH` and `DERIVE` on a TMDb key,
        `INDEX` on an embedder, `CURATE` on an `LLMClient` -- so "this
        deployment cannot run that kind" is wiring a test has to be able to see.
        The other five are in every build: there is no optional process resource
        behind a triggered sync or a bulk import, only the adapter factory and
        the outbound client every root already builds.
        """
        return frozenset(self._concurrency)

    async def recover(self) -> int:
        """Return abandoned claims to `pending`.

        Returns how many. PRD 08: *"Abandoned claims are recovered on a lease"*,
        which takes two things:

        - An age threshold, not everything. `requeue_running()`'s
          `older_than_seconds=0.0` default requeues every `running` row, which
          at two workers means a restart steals the other's live claims, and
          under concurrency inside one process it would steal its own.
        - Called repeatedly, not once at startup. Recovery that only runs when a
          process starts cannot recover a process that died and did not come
          back.

        Safe against a *live* claim because `_heartbeat` moves
        `jobs.updated_at` for everything in flight, so a claim older than the
        lease really is one nobody is working on.
        """
        async with self._scopes() as scope:
            requeued = await scope.queue.requeue_running(older_than_seconds=self._lease_seconds)
            await scope.commit()
        if requeued:
            logger.warning(
                "requeued {count} jobs left running for more than {lease}s by a process "
                "that stopped heartbeating",
                count=requeued,
                lease=self._lease_seconds,
            )
        return requeued

    async def run_once(self) -> int:
        """Claim and run up to `batch_size` jobs, concurrently.

        Returns how many ran.

        Claims only the kinds this worker has a handler for. Claiming
        everything and discovering the gap afterwards would either crash on
        the lookup or park work whose only problem is that it was offered to
        the wrong process -- and a job parked that way needs a human to
        release it.

        `asyncio.wait`, never a `TaskGroup` and never `gather`. A bug in
        one handler must cost its own job: a task group cancels its siblings on
        the first escape, which would turn one poisoned job into `N` claims
        abandoned mid-write, and `gather(return_exceptions=False)` returns while
        the siblings are still running and unawaited. The first escaping
        exception is re-raised after every task has settled, so the lane above
        still sees it and nothing is left in flight.
        """
        heartbeat = asyncio.create_task(self._heartbeat(), name="usher.jobs.heartbeat")
        try:
            return await self._pass()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _pass(self) -> int:
        total = 0
        running: set[asyncio.Task[None]] = set()
        failure: BaseException | None = None
        try:
            while total < self._batch_size and failure is None:
                if len(running) > self._low_water:
                    done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    failure = _first_failure(done)
                    continue
                room = min(self._max_in_flight - len(running), self._batch_size - total)
                claimed = await self._claim(room)
                if not claimed:
                    break
                total += len(claimed)
                running |= {
                    asyncio.create_task(self._run_in_scope(job), name=f"usher.job.{job.kind.value}")
                    for job in claimed
                }
                if len(claimed) < room:
                    # A short claim means the queue is drained, so stop asking and
                    # let the pool finish. Without this the pass issues a second,
                    # empty claim immediately -- `create_task` only *schedules*, so
                    # not one of the jobs just claimed has started yet.
                    break
        except BaseException:
            # Including `CancelledError`, which is how a lane is stopped. A
            # claim left running by a task nobody awaited is an orphan the
            # lease has to clean up minutes later; cancelling and awaiting here
            # lets each job's own `finally` fail or complete it now.
            for task in running:
                task.cancel()
            if running:
                await asyncio.wait(running)
            raise
        if running:
            done, _ = await asyncio.wait(running)
            failure = failure if failure is not None else _first_failure(done)
        if failure is not None:
            raise failure
        return total

    async def _claim(self, limit: int) -> list[Job]:
        """One claim, on its own scope, committed before it is returned.

        The commit that makes the claim durable while the work runs. It has to
        happen before the first handler, and the scope closes here rather than
        being held for the batch: a session kept
        open across the slowest upstream is the transaction this design exists
        to avoid, and under concurrency it would also be a session two jobs
        could reach.
        """
        if limit <= 0:
            return []
        async with self._scopes() as scope:
            claimed = await scope.queue.claim(list(self._concurrency), limit=limit)
            # Unconditional: an empty claim on a polling worker still ends a
            # transaction that would otherwise hold its snapshot open across
            # the poll interval.
            await scope.commit()
        return claimed

    async def _run_in_scope(self, job: Job) -> None:
        """One job, gated by its kind's ceiling, on a session of its own.

        The gate is taken *before* the scope is opened, so a kind waiting at its
        ceiling is not also holding a connection out of the pool. That matters
        most for the kinds whose ceiling is 1 -- an `INDEX` backlog would
        otherwise pin `max_in_flight` connections doing nothing.

        `_in_flight` is joined before the gate, not after it, and the two
        spellings differ by a duplicate execution. The claim was committed the
        moment it was claimed, so from the queue's point of view this row is
        `running` while it waits its turn -- and a wait at a ceiling of one can
        outlast the lease. Heartbeated only from the gate inwards, that job ages
        out and another worker takes a claim this one still intends to run. "In
        flight" means claimed and not yet settled.
        """
        self._in_flight.add(job.id)
        try:
            async with self._gates[job.kind], self._scopes() as scope:
                await self._run(job, scope)
        finally:
            self._in_flight.discard(job.id)

    async def _heartbeat(self) -> None:
        """Keep every in-flight claim out of `recover()`'s reach.

        Without this the lease has to exceed the longest job -- and the longest
        job here is a `bootstrap` phase that runs for hours, which would make
        the orphan window hours too. With it the lease is a property of the
        *process being alive* rather than of what it happens to be running.

        A failed beat is logged and not fatal: the worst case is that a claim
        ages past its lease and is re-run, and redelivery is safe by
        construction (PRD 08).
        """
        interval = self._lease_seconds / HEARTBEAT_FRACTION
        while True:
            await asyncio.sleep(interval)
            held = tuple(self._in_flight)
            if not held:
                continue
            try:
                async with self._scopes() as scope:
                    await scope.queue.touch(held)
                    await scope.commit()
            except Exception as exc:
                logger.warning(
                    "could not heartbeat {count} claims: {error}", count=len(held), error=str(exc)
                )

    async def _run(self, job: Job, scope: JobScope) -> None:
        started = time.perf_counter()
        with _tracer.start_as_current_span(f"job.{job.kind.value}", links=_links_for(job)) as span:
            span.set_attribute("usher.job.kind", job.kind.value)
            span.set_attribute("usher.job.key", job.key)
            span.set_attribute("usher.job.attempts", job.attempts)
            try:
                try:
                    await scope.handlers[job.kind](job)
                except PortDataMalformed as exc:
                    span.set_attribute("usher.job.parked", True)
                    await self._fail(job, exc, scope, retryable=False)
                except UsherPortError as exc:
                    await self._fail(job, exc, scope, retryable=True)
                except Exception:
                    # Records and re-raises; it does not handle. A bug is still not
                    # an upstream failure, still does not reach `fail()`, and still
                    # leaves this pass by the `raise` below.
                    span.set_attribute("usher.job.crashed", True)
                    logger.opt(exception=True).error(
                        "{kind} job {key} crashed; the claim stays running until the lease "
                        "expires and another worker recovers it",
                        kind=job.kind.value,
                        key=job.key,
                    )
                    raise
                else:
                    await scope.queue.complete(job.id)
                    # Per job, not per batch: a crash nineteen jobs into twenty
                    # must not re-run the nineteen. Redelivery is safe by
                    # construction (PRD 08), but doing it for free is not.
                    await scope.commit()
                    # The last thing that happens: every write this unit of work made
                    # -- the handler's own, the `BACKFILL` requests it staged, and the
                    # `DELETE` that completed the job -- is committed above, so a
                    # client told now can refetch anything the frame names.
                    await scope.events.flush()
            finally:
                # The clear at the end of this job, and it is here rather than on the
                # two `except` arms because a bug that is not a `UsherPortError`
                # propagates past both by design.
                scope.events.discard()
        _job_duration.record(time.perf_counter() - started, {"kind": job.kind.value})

    async def _fail(
        self, job: Job, exc: UsherPortError, scope: JobScope, *, retryable: bool
    ) -> None:
        # `str(exc)`, never the exception object and never a payload: PRD 08's
        # credentials-are-never-logged rule applies to a column an operator reads and to
        # this log line alike.
        retry_after_seconds = exc.retry_after if isinstance(exc, PortRateLimited) else None
        outcome = await scope.queue.fail(
            job.id, error=str(exc), retryable=retryable, retry_after_seconds=retry_after_seconds
        )
        await scope.commit()
        logger.warning(
            "{kind} job {key} failed ({attempts} attempts, {disposition}): {error}",
            kind=job.kind.value,
            key=job.key,
            # `None` when the id is unknown -- a worker whose claim a restart
            # requeued out from under it still has to log rather than crash on
            # an attribute of nothing.
            attempts=None if outcome is None else outcome.attempts,
            disposition="unknown" if outcome is None else outcome.status.value,
            error=str(exc),
        )


class WorkerLoop:
    """The poll loop around a `JobWorker`, shared by both composition roots.

    `usher work` and the server's worker lane are the same deployment one
    setting apart, so the three things that decide what a pass *means* -- when
    recovery is allowed to run, what a crashed pass costs, and how long a pass
    that claimed nothing waits -- belong in one place rather than in two that
    have to be compared. What differs between the roots is passed in: the
    worker, the per-pass refresh, where a recovery count goes, and the line a
    crash is recorded on.

    `worker` is a callable and is resolved on the first pass, because the lane
    needs its first database call to happen inside the loop rather than at
    process start.
    """

    def __init__(
        self,
        worker: Callable[[], Awaitable[JobWorker]],
        *,
        lease_seconds: float,
        idle_seconds: float,
        refresh: Callable[[], Awaitable[None]],
        recovered: Callable[[int], None],
        failure: str,
    ) -> None:
        self._worker = worker
        self._built: JobWorker | None = None
        self._lease_seconds = lease_seconds
        self._idle_seconds = idle_seconds
        self._refresh = refresh
        self._recovered = recovered
        self._failure = failure
        # `-inf`, never `0.0`: `time.monotonic()` is seconds since boot on Linux,
        # so a `0.0` origin suppresses recovery for the first half lease of host
        # uptime -- exactly when a stack coming up with the machine is holding the
        # previous boot's orphans.
        self._throttled_at = float("-inf")

    async def pass_once(self) -> int:
        """One pass, unguarded.

        Returns how many jobs ran.

        Recovery is throttled to half the lease because it is an `UPDATE`
        scanning `status = 'running'` and between leases there is nothing to
        find. It runs *before* the claim, so a dead process's abandoned claims
        are this pass's work rather than nobody's.
        """
        if self._built is None:
            self._built = await self._worker()
        now = time.monotonic()
        if now - self._throttled_at >= self._lease_seconds / 2:
            self._recovered(await self._built.recover())
            self._throttled_at = now
        ran = await self._built.run_once()
        await self._refresh()
        return ran

    async def guarded_pass(self) -> int:
        """`pass_once`, with a crashed pass costing the pass rather than the process.

        Returns `0` on a crash, which is what makes the caller sleep instead of
        hot-looping a failing pass.

        `logger.exception`, never `logger.warning`: an arm that swallowed a bug
        and logged a *message* would turn a dead worker -- at least visible --
        into a healthy-looking one silently retrying a deterministic fault.
        `configure_logging` sets `diagnose=False`, so the frames carry no locals.

        `Exception`, never `BaseException`: `CancelledError` is how a SIGINT
        reaches this loop, and catching it would build a worker that cannot be
        stopped out of the arm that stops it dying.
        """
        try:
            return await self.pass_once()
        except Exception as exc:
            logger.exception(self._failure, error=str(exc))
            return 0

    async def run(self, *, after: Callable[[int], None] | None = None) -> None:
        """Guarded passes until cancelled, sleeping after one that claimed nothing.

        `after` is the root's chance to report a pass it has just seen.
        """
        while True:
            ran = await self.guarded_pass()
            if after is not None:
                after(ran)
            if ran == 0:
                await asyncio.sleep(self._idle_seconds)


def _links_for(job: Job) -> list[Link]:
    """A `Link` to the span that enqueued this job, if it recorded one.

    `extract` yields a `Context` holding a `NonRecordingSpan` with
    `is_remote=True`, which is exactly what a `Link` wants. An unparseable or
    absent `traceparent` yields no link rather than raising -- a job is not
    worth failing over its own telemetry, and `traceparent` is null for
    everything a background sweep enqueues.
    """
    if not job.traceparent:
        return []
    context = _propagator.extract({"traceparent": job.traceparent})
    span_context = trace.get_current_span(context).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []


def _first_failure(done: "set[asyncio.Task[None]]") -> BaseException | None:
    """The first exception among settled tasks, or `None`.

    `task.exception()` rather than `task.result()`: reading the exception is
    what marks it retrieved, so a job that failed and whose sibling failed too
    does not also produce CPython's "exception was never retrieved" line at GC
    time.
    """
    for task in done:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            return error
    return None
