"""The queue's consumer (PRD 08's job-reliability rules).

Six properties that are not obvious from "it calls a handler":

1. **The claim is committed before the handler runs.** `JobQueue`'s own
   docstring requires it, and what it buys is that the claim is *durable*
   while the work is in flight: a process killed mid-job leaves a `running`
   row that `requeue_running` can recover, rather than a claim that rolls
   back into `pending` with no record that anything ever tried. It also
   keeps a transaction from spanning the whole job -- at a queue the size of
   this library that is a transaction held open for as long as the slowest
   upstream, with every claimed row locked behind it.
2. **`PortDataMalformed` parks immediately.** Its own docstring: "the
   upstream answered, and the answer was wrong. Retrying does not help, so a
   caller parks the work rather than backing off." Every other
   `UsherPortError` backs off, and parks at the queue's attempt ceiling.
3. **Anything that is not a `UsherPortError` propagates.** A bug in a
   handler is not an upstream failure; recording it as one turns a crash
   into five retries and a park whose message points at the wrong thing,
   with the worker still running and nothing ever loud.
4. **A failing job costs its own job, not the batch.** Each job is completed
   and committed as it finishes, so a crash halfway through a batch cannot
   un-complete the half that worked -- and a bug that escapes one job's task
   does not cancel its siblings, which is why this module uses
   `asyncio.wait` rather than a `TaskGroup`.
5. **Jobs run concurrently, in a bounded pool, each on its own scope.** See
   below; this is what M9's W1 changed and why.
6. **A live claim is heartbeated and an abandoned one is leased.** Recovery
   is `recover()` on an age threshold, called repeatedly, rather than
   `startup()` on `older_than_seconds=0.0`, called once.

## The scope, and why a session could not simply be passed in

`run_once` used to claim a batch of `batch_size` and **await them one at a
time**. In-flight upstream requests per process: exactly one. M9's S3
measured that over 130,334 live TMDb requests: three worker processes reached
19.76 rps against a token bucket configured at 10 rps **per process** that was
never the binding constraint on any of them, and per-worker throughput *rose*
from 6.59 to 7.72 rps when one of the three died. The ceiling was the loop.

The `gather` is the easy part of removing it. The hard part is that
`AsyncSession` is **not concurrency-safe** and every handler's repositories are
bound to one, so concurrent jobs need a session, a commit, a set of handlers
and an event buffer *each*. Hence `JobScope`: the worker is constructed with a
**factory** rather than with a bound queue and commit, and it opens one scope
per claim and one per job. `services/` may depend only on `domain/` and
`ports/` (ADR-0009), so the factory is a plain callable returning an async
context manager and the composition root is what knows it opens a session --
the same reason `commit` was injected before rather than a session being
passed in.

The event buffer moved into the scope for the same reason and it is not
symmetry: `DeferredEventPublisher` is emptied by `flush()` on success and by
`discard()` on failure, so one buffer shared by two in-flight jobs means a
failing job discards a *surviving* job's frames -- an enriched title no client
is ever told about. `tests/unit/test_services_jobs.py::
test_one_jobs_events_are_not_discarded_by_another_jobs_failure` is that case,
and it is deliberately red only against the intermediate implementation
(concurrency over one shared buffer) because with one job in flight the state
is unreachable.

## The pool is fed, not batched

A `gather` over a fixed batch of 20 waits for the slowest of 20 before
claiming the next 20, which is a straggler stall a continuously-fed pool does
not have -- and it leaves 20 claims outstanding when only `max_in_flight` of
them can run, which is 20 rows a crash orphans instead of `max_in_flight`. So
a pass claims **what the pool has room for**, and tops up when in-flight falls
to the low-water mark. `batch_size` is what bounds one *pass* (so the lane can
refresh its gauges and the CLI can say `--once`), no longer what one claim
asks for.

The worker's span is a **root with a link** to whatever enqueued the job,
never a child. The enqueueing request has usually already returned, and a
child span of a finished parent misstates causality and grows a branch on a
closed trace minutes after it ended.
"""

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

from usher.domain.jobs import Job, JobKind
from usher.ports.errors import PortDataMalformed, PortRateLimited, UsherPortError
from usher.ports.jobs import JobQueue
from usher.services.events import DeferredEventPublisher

Handler = Callable[[Job], Awaitable[None]]

#: How long a claim may sit in `running` without being heartbeated before any
#: worker may take it back. **This is what makes orphan recovery work at more
#: than one worker at all.** `JobWorker.startup()` used to call
#: `requeue_running()` with the port's `older_than_seconds=0.0` default, which
#: requeues *everything* currently running -- correct at exactly one worker and,
#: at two, a restart that steals the other's live claims. M9's S3 hit the
#: consequence: one of three workers died holding 20 claims and there was **no
#: way to recover them without corrupting the other two**, so the 20 were
#: written off. A lease plus a heartbeat is what turns that dead end into a
#: wait, and the wait is bounded by this number.
DEFAULT_LEASE_SECONDS: Final = 300.0

#: How much of the lease may pass between heartbeats. A third, so two
#: consecutive missed beats -- a stalled event loop, a slow database -- still
#: leave a margin before another worker may take the claim.
HEARTBEAT_FRACTION: Final = 3.0

#: Per-kind ceilings on jobs in flight, `None` meaning "whatever the deployment
#: configured globally" (`Settings.job_concurrency`).
#:
#: **One global number would be wrong**, and each entry below names the
#: measurement it comes from rather than a preference. Total over `JobKind` on
#: purpose: a new member with no entry fails
#: `tests/unit/test_config.py::test_the_worker_concurrency_settings_have_the_measured_defaults`
#: rather than silently inheriting a number chosen for something else -- filed
#: there because that case also pins the global this table resolves against,
#: and the two lists are only meaningful beside each other.
#:
#: - **`ENRICH`, and the global default with it.** Network-bound against TMDb.
#:   Little's law over what S3 *measured*: p95 HTTP 0.4267 s plus ~0.033 s of
#:   Postgres bookkeeping per job (S2's one-worker 10.38 rps against its own
#:   0.0637 s mean HTTP) is a p95 job of ~0.46 s, so holding ADR-0005's ~25 rps
#:   takes ~11.5 jobs in flight. `Settings.job_concurrency` defaults to 12.
#: - **`MATCH`, `WATCH_HISTORY`, `WATCH_WRITEBACK`.** A *household* media
#:   server, not a CDN-backed public API, and **this repository has never
#:   measured one under concurrent load** -- so the number is deliberately a
#:   small constant rather than the global, and it says so. `handlers.py` prices
#:   the upstream at 1-5 s per request; four in flight is up to four concurrent
#:   requests against a machine somebody is also watching television on.
#: - **`INDEX` = 1.** CPU-bound through `fastembed`, and measured:
#:   `.claude/rules/search-and-embeddings.md` records ~8,000-10,700 tokens/s
#:   held **flat across the whole size range**, with the best batch at 16 and
#:   flat to 64. A tokens/s ceiling set by the CPU is not raised by asking for
#:   it from more coroutines; the parallelism unit is already the batch, and
#:   `Settings.embedding_batch_size` is the knob that moves it.
#: - **`CURATE` = 1.** PRD 06 budgets *one modest completion per household per
#:   day*, and M8 measured the reference endpoint with no headroom left: pool
#:   600 renders ~12,540 prompt tokens, which with `llm_max_output_tokens=2048`
#:   leaves **56 tokens** under `max_model_len`. Two concurrent generations
#:   double KV-cache demand on a server measured at its context ceiling.
#: - **`SYNC` = 1.** One walk of the one measured library is 1,126,674 items;
#:   two at once double the request rate against that household server, and
#:   ADR-0015's retraction ceiling is computed per run, so two overlapping runs
#:   each see half the retractions and neither trips it.
#: - **`BOOTSTRAP` = 1.** `BulkCatalogRepository.bulk_load_window` **commits
#:   the caller's session** -- the one documented exception on that port -- and
#:   asks for a session carrying no unrelated pending work. Two phases at once
#:   also write the same destination tables from two staging copies.
#: - **`DERIVE` = 4.** ⚠️ **Not measured — and not the only one: the
#:   `MATCH`/`WATCH_HISTORY`/`WATCH_WRITEBACK` bullet above says in as many
#:   words that this repository has never measured a household server under
#:   concurrent load, so four of the nine entries here are unmeasured, not
#:   one.** The
#:   honest statement is that it is derived from a *budget* rather than from a
#:   throughput: derivation is pure Postgres (a JSONB read and three writes, no
#:   network, no model), so its ceiling is what the connection pool can serve
#:   without starving the API in the in-process lane -- four of
#:   `Settings.db_pool_size`'s twenty. The measurement that would replace it is
#:   derive jobs/s against 1, 2, 4 and 8 in flight on one pool; nothing in this
#:   repository has run it.
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
        JobKind.BOOTSTRAP: 1,
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
        # **The registration list and the concurrency table are one object.**
        # `run_once` claims `list(self._concurrency)`, so a kind this worker
        # cannot run cannot be claimed, and a kind it can run cannot be missing
        # a ceiling. `composition.worker_kinds` builds it, and
        # `test_composition.py` asserts it agrees with the handler map in every
        # one of the eight provider/embedder/client configurations.
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

        A read-only view rather than a test reaching into `_concurrency`, and
        the property that assertion needs is the one `run_once` relies on:
        **four of the nine kinds are registered conditionally** by
        `composition.build_worker` -- `ENRICH` and `DERIVE` on a TMDb key,
        `INDEX` on an embedder, `CURATE` on an `LLMClient` -- so "this
        deployment cannot run that kind" is wiring a test has to be able to
        see, and `MATCH`, `WATCH_HISTORY`, `WATCH_WRITEBACK`, `SYNC` and
        `BOOTSTRAP` are the five in every build. `SYNC` joined them in M9's
        E3 and `BOOTSTRAP` in E5: there is no optional process resource
        behind a triggered sync or a bulk import, only the adapter factory
        and the outbound client every root already builds, so both are
        registered exactly as unconditionally as the other three.
        """
        return frozenset(self._concurrency)

    async def recover(self) -> int:
        """Return **abandoned** claims to `pending`. Returns how many.

        PRD 08's *"startup requeues anything left `in_progress`"*, corrected in
        two ways that are the same correction:

        - **It is an age threshold, not everything.** `requeue_running()`'s
          `older_than_seconds=0.0` default requeues every `running` row, which
          at two workers means a restart steals the other's live claims. With
          concurrency inside one process it would steal *its own*.
        - **It is called repeatedly, not once at startup.** Recovery that only
          runs when a process starts cannot recover a process that died and did
          not come back -- which is precisely S3's twenty orphans, unrecoverable
          because the only lever also corrupted the two surviving workers.

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
        """Claim and run up to `batch_size` jobs, concurrently. Returns how
        many ran.

        Claims only the kinds this worker has a handler for. Claiming
        everything and discovering the gap afterwards would either crash on
        the lookup or park work whose only problem is that it was offered to
        the wrong process -- and a job parked that way needs a human to
        release it.

        **`asyncio.wait`, never a `TaskGroup` and never `gather`.** A bug in
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
                    # **A short claim means the queue is drained, so stop
                    # asking and let the pool finish.** Without this the pass
                    # issues a second, empty claim immediately -- and it really
                    # is immediate, because `create_task` only *schedules*, so
                    # not one of the jobs just claimed has started yet. That is
                    # a wasted round trip and a wasted commit on every pass of
                    # a nearly-empty queue, which is the shape a household
                    # deployment is in almost always. Anything enqueued while
                    # these run (an `enrich` staging its `index` and `derive`)
                    # is claimed by the next pass, which follows with no sleep
                    # because this one ran something.
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

        The commit that makes the claim durable while the work runs -- see the
        module docstring. It has to happen before the first handler, and the
        scope closes here rather than being held for the batch: a session kept
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

        The gate is taken **before** the scope is opened, so a kind waiting at
        its ceiling is not also holding a connection out of the pool. That
        matters most for the kinds whose ceiling is 1 -- an `INDEX` backlog
        would otherwise pin `max_in_flight` connections doing nothing.

        ⚠️ **`_in_flight` is joined *before* the gate, not after it, and the
        two spellings differ by a lost job.** The claim was committed the
        moment it was claimed, so from the queue's point of view this row is
        `running` while it waits its turn -- and a wait can be long: twenty
        `index` jobs at a ceiling of one, thirty seconds each, is ten minutes
        for the last of them, well past the 300 s lease. Heartbeated only from
        the gate inwards, that job ages out and **another worker takes a claim
        this one still intends to run**, which is a duplicate execution rather
        than a lost one. "In flight" means claimed and not yet settled.
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
        job here is a `bootstrap` phase measured in hours, which would make the
        orphan window hours too. With it the lease is a property of the
        *process being alive* rather than of what it happens to be running, so
        S3's twenty orphans would have come back in five minutes.

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
                else:
                    await scope.queue.complete(job.id)
                    # Per job, not per batch: a crash nineteen jobs into twenty
                    # must not re-run the nineteen. Redelivery is safe by
                    # construction (PRD 08), but doing it for free is not.
                    await scope.commit()
                    # ADR-0033, and it is the last thing that happens: every
                    # write this unit of work made -- the handler's own, the
                    # `BACKFILL` requests it staged, and the `DELETE` that
                    # completed the job -- is committed above, so a client
                    # told now can refetch anything the frame names. Before
                    # `complete()` it could not: the two enqueues and the
                    # completion were still open on this session.
                    await scope.events.flush()
            finally:
                # The clear at the end of this job, and it is here rather than
                # on the two `except` arms because a bug that is not a
                # `UsherPortError` propagates past both by design. **This
                # buffer is the scope's**, so it holds this job's frames and
                # nothing else -- shared, the line below would empty a
                # concurrent job's.
                scope.events.discard()
        _job_duration.record(time.perf_counter() - started, {"kind": job.kind.value})

    async def _fail(
        self, job: Job, exc: UsherPortError, scope: JobScope, *, retryable: bool
    ) -> None:
        # `str(exc)`, never the exception object and never a payload: PRD 08's
        # credentials-are-never-logged rule applies to a column an operator
        # reads and to this log line alike.
        #
        # `isinstance`, not `getattr(exc, "retry_after", None)`: the latter is
        # how a future exception member accidentally opts into a behaviour
        # nobody chose. `PortRateLimited` is the one member of the taxonomy
        # that carries the attribute; naming it is what keeps that true
        # tomorrow rather than only today.
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
    time -- the shape `api/lanes._guard` exists for, arriving through a task.
    """
    for task in done:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            return error
    return None
