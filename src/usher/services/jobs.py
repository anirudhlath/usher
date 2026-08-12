"""The queue's consumer (PRD 08's job-reliability rules).

Four properties that are not obvious from "it calls a handler":

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
4. **A failing job costs its own job, not the batch.** The try/except is
   inside the loop, so nineteen good jobs claimed alongside one poisoned one
   still run -- and each is completed and committed as it finishes, so a
   crash halfway through a batch cannot un-complete the half that worked.

The worker's span is a **root with a link** to whatever enqueued the job,
never a child. The enqueueing request has usually already returned, and a
child span of a finished parent misstates causality and grows a branch on a
closed trace minutes after it ended.

`services/` may depend only on `domain/` and `ports/` (ADR-0009), so
`commit` is injected rather than a session being passed in -- the same shape
`ReconcileService` and `BootstrapService` already use.
"""

import time
from collections.abc import Awaitable, Callable

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.trace import Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from usher.domain.jobs import Job, JobKind
from usher.ports.errors import PortDataMalformed, PortRateLimited, UsherPortError
from usher.ports.events import EventPublisher, NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.services.events import DeferredEventPublisher

Handler = Callable[[Job], Awaitable[None]]

_tracer = trace.get_tracer("usher.jobs")
_meter = metrics.get_meter("usher.jobs")
_job_duration = _meter.create_histogram(
    "usher.jobs.duration", unit="s", description="Wall time per job"
)
_propagator = TraceContextTextMapPropagator()


class JobWorker:
    def __init__(
        self,
        queue: JobQueue,
        commit: Callable[[], Awaitable[None]],
        *,
        events: EventPublisher | None = None,
        batch_size: int = 20,
    ) -> None:
        self._queue = queue
        self._commit = commit
        self._batch_size = batch_size
        self._handlers: dict[JobKind, Handler] = {}
        self._events = DeferredEventPublisher(NullEventPublisher() if events is None else events)

    @property
    def events(self) -> EventPublisher:
        """The publisher every service this worker's handlers reach must hold.

        Not the one this worker was constructed with -- the buffer wrapped
        around it. A handler holding the bare publisher would offer its
        frames from inside `_run`, which is
        [ADR-0033](../../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)'s
        residual window, and the rule would be back to five hand-written
        comments. `composition.build_worker` hands *this* to every service it
        builds, and the wrapping happening here rather than there is what
        leaves one construction site in `src/` instead of one per lane.
        """
        return self._events

    def register(self, kind: JobKind, handler: Handler) -> None:
        self._handlers[kind] = handler

    @property
    def registered_kinds(self) -> frozenset[JobKind]:
        """Exactly what `run_once` will claim.

        A read-only view rather than a test reaching into `_handlers`, and
        the property that assertion needs is the one `run_once` relies on:
        **four of the eight kinds are registered conditionally** by
        `composition.build_worker` -- `ENRICH` and `DERIVE` on a TMDb key,
        `INDEX` on an embedder, `CURATE` on an `LLMClient` -- so "this
        deployment cannot run that kind" is wiring a test has to be able to
        see, and `MATCH`, `WATCH_HISTORY`, `WATCH_WRITEBACK` and `SYNC` are the
        four in every build. `SYNC` joined them in M9's E3: there is no
        optional process resource behind a triggered sync, only the adapter
        factory every root already builds, so it is registered exactly as
        unconditionally as the other three. A mutable dict handed out would
        let a caller register a handler the worker never knew about, which is
        the same silent gap the other way round.
        """
        return frozenset(self._handlers)

    async def startup(self) -> int:
        """PRD 08's "startup requeues anything left `in_progress`".

        The default `older_than_seconds=0.0` requeues **everything** currently
        `running`, which is correct at exactly one worker -- the deployment
        shape M4 ships, and the same single-replica assumption the container's
        `alembic upgrade head` on start already makes. A second worker process
        would steal a live claim with it, and the argument to pass instead is
        already on the port.
        """
        requeued = await self._queue.requeue_running()
        await self._commit()
        if requeued:
            logger.warning(
                "requeued {count} jobs left running by a previous process", count=requeued
            )
        return requeued

    async def run_once(self) -> int:
        """Claim and run up to `batch_size` jobs. Returns how many ran.

        Claims only the kinds this worker has a handler for. Claiming
        everything and discovering the gap afterwards would either crash on
        the lookup or park work whose only problem is that it was offered to
        the wrong process -- and a job parked that way needs a human to
        release it.
        """
        claimed = await self._queue.claim(list(self._handlers), limit=self._batch_size)
        # The commit that makes the claim durable while the work runs -- see
        # the module docstring. It has to happen between the claim and the
        # first handler, not after the loop. Unconditional: an empty claim on
        # a polling worker still ends a transaction that would otherwise hold
        # its snapshot open across the poll interval.
        await self._commit()
        for job in claimed:
            await self._run(job)
        return len(claimed)

    async def _run(self, job: Job) -> None:
        started = time.perf_counter()
        with _tracer.start_as_current_span(f"job.{job.kind.value}", links=_links_for(job)) as span:
            span.set_attribute("usher.job.kind", job.kind.value)
            span.set_attribute("usher.job.key", job.key)
            span.set_attribute("usher.job.attempts", job.attempts)
            try:
                try:
                    await self._handlers[job.kind](job)
                except PortDataMalformed as exc:
                    span.set_attribute("usher.job.parked", True)
                    await self._fail(job, exc, retryable=False)
                except UsherPortError as exc:
                    await self._fail(job, exc, retryable=True)
                else:
                    await self._queue.complete(job.id)
                    # Per job, not per batch: a crash nineteen jobs into twenty
                    # must not re-run the nineteen. Redelivery is safe by
                    # construction (PRD 08), but doing it for free is not.
                    await self._commit()
                    # ADR-0033, and it is the last thing that happens: every
                    # write this unit of work made -- the handler's own, the
                    # `BACKFILL` requests it staged, and the `DELETE` that
                    # completed the job -- is committed above, so a client
                    # told now can refetch anything the frame names. Before
                    # `complete()` it could not: the two enqueues and the
                    # completion were still open on this session.
                    await self._events.flush()
            finally:
                # The clear between jobs, and it is here rather than on the
                # two `except` arms because a bug that is not a
                # `UsherPortError` propagates past both by design. Without
                # it a crashed job's frames wait in the buffer until the
                # *next* successful job flushes them -- on a worker built
                # once per process and polling for days.
                self._events.discard()
        _job_duration.record(time.perf_counter() - started, {"kind": job.kind.value})

    async def _fail(self, job: Job, exc: UsherPortError, *, retryable: bool) -> None:
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
        outcome = await self._queue.fail(
            job.id, error=str(exc), retryable=retryable, retry_after_seconds=retry_after_seconds
        )
        await self._commit()
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
