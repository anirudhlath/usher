"""The worker loop, against `FakeJobQueue`."""

import asyncio
import contextlib
import contextvars
import inspect
import io
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.contract.job_queue_contract import ClaimWindow, overlapping
from tests.fakes.job_queue import FakeJobQueue
from usher.domain.jobs import Job, JobKind, JobPriority, JobStatus
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable, UsherPortError
from usher.ports.events import (
    ClientEvent,
    ClientEventKind,
    EventPublisher,
    NullEventPublisher,
)
from usher.ports.jobs import JobRequest
from usher.services.events import DeferredEventPublisher
from usher.services.jobs import (
    DEFAULT_LEASE_SECONDS,
    JobScope,
    JobWorker,
    _links_for,
)
from usher.telemetry import current_traceparent


class _RecordingPublisher(EventPublisher):
    """The bus's stand-in, writing into the same log the commits do.

    One shared list rather than a counter beside a counter, for `_Fixture`'s
    own reason: the rule is about *ordering*, and "one publish, one commit" is
    what both orders produce.
    """

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.offered: list[ClientEvent] = []
        self.raises: BaseException | None = None

    async def publish(self, event: ClientEvent) -> None:
        self._log.append("publish")
        self.offered.append(event)
        if self.raises is not None:
            raise self.raises


class _RecordingQueue(FakeJobQueue):
    """`FakeJobQueue`, plus a note in the shared log at each completion.

    The interleaving is `complete -> commit -> publish`, and the completion
    is the one of the three the *queue* owns -- so without this the case
    could only assert that a publish came after some commit, which is also
    what a flush before `complete()` produces.
    """

    def __init__(self, log: list[str], *, max_attempts: int, backoff_seconds: float) -> None:
        super().__init__(max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        self._log = log

    async def complete(self, job_id: uuid.UUID) -> None:
        self._log.append("complete")
        await super().complete(job_id)

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> Job | None:
        self._log.append("fail")
        return await super().fail(
            job_id, error=error, retryable=retryable, retry_after_seconds=retry_after_seconds
        )


class _Fixture:
    """Worker, queue, and one event log recording what happened in order.

    The log, rather than two counters: every ordering property below --
    commit before the first handler, a commit after each completion, a
    commit after a failure -- is a statement about *sequence*, and a pair of
    totals cannot distinguish "committed, then ran" from "ran, then
    committed".
    """

    def __init__(
        self,
        *,
        batch_size: int = 20,
        max_attempts: int = 5,
        concurrency: int = 4,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.log: list[str] = []
        self.queue = _RecordingQueue(self.log, max_attempts=max_attempts, backoff_seconds=1.0)
        self.handled: list[Job] = []
        self.bus = _RecordingPublisher(self.log)
        #: Every event a handler in this fixture handed to its scope's buffer.
        #: The premise for every assertion about what was *not* offered: an
        #: absence read out of a handler that published nothing is vacuous.
        self.raised: list[ClientEvent] = []
        self._handlers: dict[JobKind, Callable[[Job], Awaitable[None]]] = {
            JobKind.ENRICH: self._handle
        }
        #: The buffer of the scope whichever handler is running right now, so
        #: `publish_for` can reach the publisher the *worker* handed it rather
        #: than one of the fixture's own -- that binding is what
        #: `composition.build_worker` makes and what a per-job buffer has to
        #: get right.
        self._scope_events: contextvars.ContextVar[DeferredEventPublisher] = contextvars.ContextVar(
            "scope_events"
        )
        #: Every scope this fixture has opened, so a case can assert there was
        #: one per job rather than one per worker.
        self.scopes: list[JobScope] = []
        # `concurrency` above 1 by default, so every case in this file runs under the
        # shape production runs under rather than under a serialised special case.
        self.worker = JobWorker(
            self._scope,
            dict.fromkeys(self._handlers, concurrency),
            max_in_flight=concurrency,
            batch_size=batch_size,
            lease_seconds=lease_seconds,
        )

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[JobScope]:
        """One scope, with a buffer of its own over the shared bus.

        The queue is shared because `FakeJobQueue` *is* the store -- one dict
        behind one event loop, with no second session to model. What is
        per-scope here is what is per-session in production: the commit, the
        handlers and the event buffer.
        """
        events = DeferredEventPublisher(self.bus)
        scope = JobScope(
            queue=self.queue, commit=self._commit, handlers=dict(self._handlers), events=events
        )
        self.scopes.append(scope)
        token = self._scope_events.set(events)
        try:
            yield scope
        finally:
            self._scope_events.reset(token)

    async def _commit(self) -> None:
        self.log.append("commit")

    async def _handle(self, job: Job) -> None:
        self.log.append(f"handle:{job.key}")
        self.handled.append(job)

    def register(self, kind: JobKind, handler: Callable[[Job], Awaitable[None]]) -> None:
        """Replace what this fixture's worker runs for `kind`.

        Every scope opened after this call carries it; a scope already open
        does not, exactly as a redeployment does not change a running job.
        """
        self._handlers[kind] = handler

    async def publish_for(self, job: Job) -> None:
        """Raise one frame naming `job`, the way `EnrichService` does."""
        event = ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"job": job.key})
        await self._scope_events.get().publish(event)
        self.raised.append(event)

    def raising(self, exc: BaseException) -> Callable[[Job], Awaitable[None]]:
        async def _handler(job: Job) -> None:
            self.log.append(f"handle:{job.key}")
            self.handled.append(job)
            raise exc

        return _handler

    def publishing(
        self, *, failing: BaseException | None = None
    ) -> Callable[[Job], Awaitable[None]]:
        """A handler that raises a client event the way `EnrichService` does.

        Through the *scope's* buffer and never through a publisher of its own,
        because that is the wiring `composition.build_worker` makes: the
        publisher a handler's service holds is the one belonging to the scope
        that built it.
        """

        async def _handler(job: Job) -> None:
            self.log.append(f"handle:{job.key}")
            self.handled.append(job)
            await self.publish_for(job)
            if failing is not None:
                raise failing

        return _handler

    async def given(self, *keys: str, kind: JobKind = JobKind.ENRICH, **fields: object) -> None:
        await self.queue.enqueue(
            [
                JobRequest(kind=kind, key=key, priority=JobPriority.NEW, **fields)  # type: ignore[arg-type]
                for key in keys
            ]
        )

    @property
    def commits_before_the_first_handler(self) -> int:
        first = next((i for i, entry in enumerate(self.log) if entry.startswith("handle:")), None)
        return len(
            [e for e in self.log[: len(self.log) if first is None else first] if e == "commit"]
        )


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def errors() -> Iterator[io.StringIO]:
    """Loguru at `ERROR` and above, for the cases whose claim is silence.

    `logger.remove()` first, because loguru's default sink is stderr and
    `logger.exception` writes a traceback there whether or not anything is
    reading -- the same shape `tests/unit/test_composition.py::warnings` uses
    one module over.
    """
    sink = io.StringIO()
    logger.remove()
    logger.add(sink, level="ERROR")
    yield sink
    logger.remove()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


# -- the happy path ---------------------------------------------------------


async def test_a_handler_runs_and_the_job_is_removed(fixture: _Fixture) -> None:
    """`requeue_running()` is the assertion with teeth, and `depth()` is not.

    `depth` counts `pending` only, so a worker that ran the handler and never
    called `complete` leaves the row `running` and reads back as an empty
    queue, which is the state PRD 08's recovery exists to clean up. Asked of
    the queue directly rather than through `JobWorker.recover()`, which passes
    an age threshold and so answers `0` against a claim made milliseconds ago
    whether it was completed or abandoned; the port's own
    `older_than_seconds=0.0` default still sees everything.
    """
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0
    assert await fixture.queue.parked() == []
    assert await fixture.queue.requeue_running() == 0, (
        "the job was left claimed rather than completed"
    )


async def test_the_handler_is_given_the_job_it_was_claimed_for(fixture: _Fixture) -> None:
    """`Job.key` is how every handler finds its work.

    A title id for `enrich`, an external id for `watch_history` -- so handing
    over anything but the claimed row makes each operate on the wrong thing.
    """
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert [job.key for job in fixture.handled] == ["t1"]
    assert fixture.handled[0].status is JobStatus.RUNNING


async def test_an_empty_queue_is_not_an_error(fixture: _Fixture) -> None:
    """A polling worker spends most of its life here."""
    assert await fixture.worker.run_once() == 0
    assert fixture.handled == []


async def test_the_claim_is_committed_before_the_work_starts(fixture: _Fixture) -> None:
    """The claim is committed before the handler runs.

    A worker that claims and works in one uncommitted transaction holds every
    claimed row's lock for the length of the batch, and leaves a process killed
    mid-job with no record that anything was tried. An ordering assertion and
    only that: this fake has no transaction, so nothing here can tell a commit
    that happened from one that mattered.
    """
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert fixture.commits_before_the_first_handler >= 1
    assert fixture.log[0] == "commit"


async def test_each_job_is_committed_as_it_finishes(fixture: _Fixture) -> None:
    """Per job, not per batch.

    A crash nineteen jobs into twenty must not re-run the nineteen. The
    `complete` entries are the queue's, and they pin the second half of
    "committed as it finishes": the commit follows the completion rather than
    merely following the handler.
    """
    await fixture.given("t1", "t2", "t3")
    await fixture.worker.run_once()
    assert fixture.log == [
        "commit",
        "handle:t1",
        "complete",
        "commit",
        "handle:t2",
        "complete",
        "commit",
        "handle:t3",
        "complete",
        "commit",
    ]


async def test_the_worker_only_claims_kinds_it_can_handle(fixture: _Fixture) -> None:
    """A worker claims only the kinds it has a handler for.

    One that claimed every kind would take work it cannot run and then crash on
    the handler lookup or park it -- and a job parked for being offered to the
    wrong process needs a human to release it.
    """
    await fixture.given("t1")
    await fixture.given("m1", kind=JobKind.MATCH)
    assert await fixture.worker.run_once() == 1
    depth = await fixture.queue.depth()
    assert depth[JobKind.MATCH] == 1, "the worker claimed work it has no handler for"
    assert depth[JobKind.ENRICH] == 0


# -- failure, which is the point --------------------------------------------


async def test_a_transient_failure_backs_the_job_off(fixture: _Fixture) -> None:
    fixture.register(JobKind.ENRICH, fixture.raising(PortUnavailable("upstream is down")))
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1
    assert await fixture.queue.parked() == []


async def test_a_backed_off_job_is_not_immediately_re_claimed(fixture: _Fixture) -> None:
    """The hot loop the backoff exists to prevent, at the worker level.

    One broken upstream must not become a request per handler invocation for as
    long as it stays broken.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortUnavailable("upstream is down")))
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert await fixture.worker.run_once() == 0, "the failed job was re-claimed with no wait"


async def test_malformed_data_parks_immediately(fixture: _Fixture) -> None:
    """`PortDataMalformed` means the upstream answered and the answer was wrong.

    Retrying does not help, so the work is parked rather than backed off; the
    alternative is five identical failures and a five-times-longer wait before
    a human sees it.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortDataMalformed("TMDb returned a list")))
    await fixture.given("t1")
    await fixture.worker.run_once()
    parked = await fixture.queue.parked()
    assert [job.key for job in parked] == ["t1"]
    assert parked[0].attempts == 1, "parked at the ceiling rather than immediately"


async def test_a_job_that_keeps_failing_is_parked_rather_than_retried_forever(
    fixture: _Fixture,
) -> None:
    """PRD 08's rule: after N attempts a job is *parked* with its error.

    Not retried forever and not silently dropped. All three outcomes are
    asserted: it stopped being claimable, it is listed, and it kept its error.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortUnavailable("still down")))
    await fixture.given("t1")
    for _ in range(5):
        await fixture.worker.run_once()
        await fixture.queue.clear_backoff()
    parked = await fixture.queue.parked()
    assert [(job.key, job.attempts) for job in parked] == [("t1", 5)]
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0
    assert await fixture.worker.run_once() == 0, "a parked job was claimed again"


async def test_a_parked_job_keeps_the_error_that_parked_it(fixture: _Fixture) -> None:
    """The whole of "not silently dropped" is this string.

    It is what an operator reads in the admin list, and `str(exc)` rather than
    the exception object because PRD 08's credentials-never-logged rule applies
    to a column as much as to a log line.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortDataMalformed("TMDb returned a list")))
    await fixture.given("t1")
    await fixture.worker.run_once()
    parked = await fixture.queue.parked()
    assert parked[0].last_error is not None
    assert "TMDb returned a list" in parked[0].last_error


async def test_a_failure_costs_its_own_job_and_not_the_batch(fixture: _Fixture) -> None:
    """One poisoned job in a claimed batch of three must not abandon the other two.

    At `batch_size=20`, a try/except outside the loop turns one bad payload
    into nineteen jobs silently returned to `pending` on every pass.
    """
    failing = fixture.raising(PortDataMalformed("bad payload"))

    async def _handle(job: Job) -> None:
        if job.key == "t2":
            await failing(job)
            return
        fixture.log.append(f"handle:{job.key}")
        fixture.handled.append(job)

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("t1", "t2", "t3")
    assert await fixture.worker.run_once() == 3
    assert [job.key for job in fixture.handled] == ["t1", "t2", "t3"]
    assert [job.key for job in await fixture.queue.parked()] == ["t2"]
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0


async def test_a_bug_in_a_handler_is_not_recorded_as_an_upstream_failure(
    fixture: _Fixture,
) -> None:
    """A `ZeroDivisionError` is not a `UsherPortError` and must propagate.

    Swallowing it into `fail(retryable=True)` turns a crash into a job that retries five
    times and then parks with a misleading error -- and the worker keeps running, so
    nothing is ever loud.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(ZeroDivisionError("bug")))
    await fixture.given("t1")
    with pytest.raises(ZeroDivisionError):
        await fixture.worker.run_once()
    assert await fixture.queue.parked() == []


async def test_a_bug_in_a_handler_records_its_traceback_and_says_which_job(
    fixture: _Fixture, errors: io.StringIO
) -> None:
    """A crash records which job was in flight, and its traceback.

    Propagating is the last thing that happens to a crash in this process, so
    what the log has to hold before the stack leaves is the two facts the stack
    alone cannot supply once the process is gone: **which job** was in flight,
    and the traceback, without anybody having had to remember `usher
    --traceback work` beforehand. `logger.opt(exception=True)` and not
    `str(exc)`, deliberately against this module's house rule for `_fail`:
    `telemetry.configure_logging` sets `diagnose=False`, which is what stops
    loguru rendering frame *locals* into the traceback.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(ZeroDivisionError("bug")))
    await fixture.given("t1")
    with pytest.raises(ZeroDivisionError):
        await fixture.worker.run_once()

    recorded = errors.getvalue()
    assert "enrich" in recorded
    assert "t1" in recorded
    # The traceback, not just the repr: the frame that raised has to be in it.
    assert "ZeroDivisionError" in recorded
    assert "Traceback (most recent call last)" in recorded


async def test_every_port_error_backs_off_rather_than_escaping(fixture: _Fixture) -> None:
    """Not just the two subclasses the other cases happen to raise.

    A worker that named `PortUnavailable` specifically would let `PortAuthFailed` and
    `PortRateLimited` escape `run_once` and kill the loop -- which is the same failure
    `ReconcileService` guards against one lane over.
    """

    class _Boom(UsherPortError):
        pass

    fixture.register(JobKind.ENRICH, fixture.raising(_Boom("something at the edge")))
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1


async def test_a_429_carrying_a_retry_after_backs_off_no_sooner_than_the_upstream_asked(
    fixture: _Fixture,
) -> None:
    """A `retry_after` hint is honoured rather than answered with the queue's guess.

    A positive control that the failure path ran (`attempts == 1`, `last_error`
    names the failure), then the number that matters: the job is not claimable
    again for at least the 300 s the upstream asked for.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortRateLimited(retry_after=300.0)))
    await fixture.given("t1")
    before = datetime.now(UTC)
    await fixture.worker.run_once()
    outcome = fixture.queue.jobs_of(JobKind.ENRICH)[0]
    assert outcome.attempts == 1
    assert outcome.last_error is not None and "rate limited" in outcome.last_error
    assert outcome.run_after is not None
    assert outcome.run_after - before >= timedelta(seconds=300), (
        f"backed off only {outcome.run_after - before} against a 300 s hint"
    )


async def test_a_claim_requeued_out_from_under_the_worker_does_not_crash(
    fixture: _Fixture,
) -> None:
    """`fail` answers `None` for an id the queue no longer knows.

    A restart requeued the claim and someone else finished it. The worker has
    nothing useful to do with that news except not crash on it, and crashing
    would take the whole loop down over a job that has already succeeded.
    """

    async def _steal_then_fail(job: Job) -> None:
        await fixture.queue.complete(job.id)
        raise PortUnavailable("upstream is down")

    fixture.register(JobKind.ENRICH, _steal_then_fail)
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert await fixture.queue.parked() == []


# -- an event is offered only after the job's own commit --------------------


async def test_an_event_a_handler_raised_is_not_offered_until_the_completion_is_committed(
    fixture: _Fixture,
) -> None:
    """An event is a statement about committed state, made the worker's property.

    The transaction still open at the instant of an `enrich` frame is
    **`JobWorker`'s**, holding the `BACKFILL` enqueues the handler staged and
    the `DELETE` that completes the job, so buffering here is the only spelling
    under which *"the client was told"* implies *"every write this unit of work
    made landed"* -- and the only one nothing can forget. An interleaving,
    never a membership check: `publish in log` and `len(offered) == 1` are both
    satisfied by the order this case exists to forbid.
    """
    fixture.register(JobKind.ENRICH, fixture.publishing())
    await fixture.given("t1")

    await fixture.worker.run_once()

    assert fixture.raised, "the handler published nothing; nothing below measures anything"
    assert fixture.log == ["commit", "handle:t1", "complete", "commit", "publish"]
    assert fixture.bus.offered == fixture.raised


async def test_a_job_that_failed_offers_nothing(fixture: _Fixture) -> None:
    """The twin of the case above, and the buffer's whole reason for existing.

    A buffer that flushed on both paths passes that one: the handler's writes
    rolled back with the job, so a frame telling a client to refetch names a
    change that never happened -- and the retry publishes a second one. The
    premise comes first, because an assertion that nothing was offered is
    vacuous against a handler that raised nothing.
    """
    fixture.register(
        JobKind.ENRICH, fixture.publishing(failing=PortUnavailable("upstream is down"))
    )
    await fixture.given("t1")

    await fixture.worker.run_once()

    assert fixture.raised, "the handler published nothing; the absence below is vacuous"
    assert fixture.bus.offered == []
    assert fixture.log == ["commit", "handle:t1", "fail", "commit"]


async def test_a_parked_job_offers_nothing_either(fixture: _Fixture) -> None:
    """`PortDataMalformed` takes the other `except` arm.

    An arm added to one and not the other is exactly the drift `_settle` was
    collapsed to prevent one service over: two arms, two cases.
    """
    fixture.register(
        JobKind.ENRICH, fixture.publishing(failing=PortDataMalformed("TMDb returned a list"))
    )
    await fixture.given("t1")

    await fixture.worker.run_once()

    assert fixture.raised, "the handler published nothing; the absence below is vacuous"
    assert fixture.bus.offered == []
    assert [job.key for job in await fixture.queue.parked()] == ["t1"]


async def test_the_buffer_is_per_job_and_not_per_pass(fixture: _Fixture) -> None:
    """A batch of two, one succeeding and one failing, offers only the first's events.

    `_run` sits inside `for job in claimed:` deliberately, and a flush hoisted
    to the end of `run_once` would publish the failed job's frame alongside the
    successful one's -- invisible to every case that claims one job.
    """
    succeeding = fixture.publishing()
    failing = fixture.publishing(failing=PortUnavailable("upstream is down"))

    async def _handle(job: Job) -> None:
        await (failing if job.key == "t2" else succeeding)(job)

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("t1", "t2")

    assert await fixture.worker.run_once() == 2

    assert [event.data["job"] for event in fixture.raised] == ["t1", "t2"]
    assert [event.data["job"] for event in fixture.bus.offered] == ["t1"]


async def test_a_crashing_handlers_event_is_not_offered_on_the_next_jobs_commit(
    fixture: _Fixture,
) -> None:
    """The clear between jobs, which no flush-ordering case can see.

    A bug that is not a `UsherPortError` propagates out of `_run` by design,
    so neither `except` arm runs -- and a buffer emptied only by those two
    keeps the crashed job's frame until the *next* successful job flushes it,
    on a worker that is built once per process (`usher work`) and lives for
    days. Two passes, because one cannot tell "dropped" from "not yet
    offered".
    """
    fixture.register(JobKind.ENRICH, fixture.publishing(failing=ZeroDivisionError("bug")))
    await fixture.given("t1")
    with pytest.raises(ZeroDivisionError):
        await fixture.worker.run_once()
    assert fixture.raised, "the crashing handler published nothing; this case measures nothing"

    fixture.register(JobKind.ENRICH, fixture.publishing())
    await fixture.given("t2")
    await fixture.worker.run_once()

    assert [event.data["job"] for event in fixture.bus.offered] == ["t2"]


async def test_a_flush_that_raises_does_not_turn_a_completed_job_into_a_failed_one(
    fixture: _Fixture,
) -> None:
    """A publisher that breaks the never-raises contract must not fail a finished job.

    The buffer calls `publish` on a path where the job is already complete and
    committed, so a raise there would take that job's `run_once` down with it and
    log a pass failure for a pass that succeeded.
    """
    fixture.bus.raises = RuntimeError("a subscriber transport blew up")
    fixture.register(JobKind.ENRICH, fixture.publishing())
    await fixture.given("t1")

    assert await fixture.worker.run_once() == 1

    assert fixture.bus.offered == fixture.raised, "the flush never reached the publisher"
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0
    assert await fixture.queue.parked() == []
    assert await fixture.queue.requeue_running() == 0, (
        "the job was left claimed rather than completed"
    )


async def test_a_worker_publishing_into_a_null_bus_completes_and_says_nothing(
    fixture: _Fixture, errors: io.StringIO
) -> None:
    """`usher work` as a separate process publishes to `NullEventPublisher`.

    A real deployment rather than a test double: the bus is in-memory, so an
    enrichment finished in another process reaches no SSE client and the
    client's next refetch gets the right answer anyway. The silence is the
    assertion -- `flush` catches whatever a broken publisher raises, because it
    runs after a commit it cannot undo, so a scope wrapping a broken bus
    completes every job and logs an `ERROR` per published event instead. The
    scope is built the way a composition root builds one, since the null
    default belongs to `build_pipeline` rather than to `JobWorker`.
    """
    queue = FakeJobQueue()
    events = DeferredEventPublisher(NullEventPublisher())

    async def _handler(job: Job) -> None:
        await events.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED))

    @asynccontextmanager
    async def _scope() -> AsyncIterator[JobScope]:
        yield JobScope(
            queue=queue,
            commit=fixture._commit,
            handlers={JobKind.ENRICH: _handler},
            events=events,
        )

    worker = JobWorker(_scope, {JobKind.ENRICH: 1}, max_in_flight=1)
    await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])

    assert await worker.run_once() == 1
    assert await queue.requeue_running() == 0, "the job was left claimed rather than completed"
    assert errors.getvalue() == "", f"the flush had nothing to publish into: {errors.getvalue()}"


# -- concurrency, which is observed overlap and never a count ---------------


class _Rendezvous:
    """`arrive()` returns once every expected handler has, or after `deadline`.

    The deadline is what makes this a test rather than a hang. The obvious
    spelling is `asyncio.Barrier`, and against a worker that awaits its jobs
    one at a time a barrier *deadlocks*: the first handler waits for a second
    that cannot start until the first returns.
    `.claude/rules/testing-discipline.md` records that trap -- a timing case
    can only ever report a timeout against it, and a case that hangs reports
    nothing. With a deadline the sequential run instead produces two disjoint,
    **recorded** windows, so the case fails on the property it is about.
    """

    def __init__(self, expected: int, *, deadline: float = 0.5) -> None:
        self._expected = expected
        self._deadline = deadline
        self._arrived = 0
        self._all_here = asyncio.Event()

    async def arrive(self) -> None:
        self._arrived += 1
        if self._arrived >= self._expected:
            self._all_here.set()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._all_here.wait(), self._deadline)


def _iou(windows: Sequence[ClaimWindow]) -> float:
    """Shared time as a fraction of the union of the windows, for the record.

    A number is what lets a reader tell "they overlapped" from "they overlapped
    by a scheduling accident of one microsecond".
    """
    latest_start = max(one.started_at for one in windows)
    earliest_end = min(one.finished_at for one in windows)
    union = max(one.finished_at for one in windows) - min(one.started_at for one in windows)
    return 0.0 if union <= 0 else round(max(0.0, earliest_end - latest_start) / union, 4)


async def test_two_jobs_in_one_batch_genuinely_overlap(fixture: _Fixture) -> None:
    """CLAUDE.md's fourth evidence rule, applied to the worker itself.

    "Two jobs completed" is also what a sequential loop produces, and that is
    the whole reason this case records intervals instead. A `run_once` that
    claimed a batch and awaited it one job at a time -- no `gather`, no
    `TaskGroup`, no semaphore -- holds in-flight work at exactly one, and is
    red here on the overlap assertion rather than on a clock: `_Rendezvous`
    gives up rather than deadlocking, so both windows are recorded and printed.
    """
    rendezvous = _Rendezvous(2)
    windows: list[ClaimWindow] = []

    async def _handle(job: Job) -> None:
        started = time.perf_counter()
        await rendezvous.arrive()
        windows.append(
            ClaimWindow(keys=(job.key,), started_at=started, finished_at=time.perf_counter())
        )

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("t1", "t2")

    assert await fixture.worker.run_once() == 2

    # The premise, before the property: an overlap assertion over a handler
    # that never ran is vacuous, and so is one over a single window.
    assert len(windows) == 2, f"the premise: both jobs ran -- {windows}"
    assert overlapping(windows), (
        f"the two jobs did not overlap, so the worker ran them one at a time: windows={windows}"
    )
    print(f"overlap: {_iou(windows):.2%} of the union of the two windows")


async def test_the_pool_is_topped_up_while_a_slow_job_is_still_running() -> None:
    """The straggler, which a `gather` over a fixed batch does not fix.

    A pass that claims `batch_size` and waits for the slowest of them before
    claiming again reintroduces a stall a continuously-fed pool does not have
    -- and holds `batch_size` claims when only `max_in_flight` of them can run,
    so a crash orphans twenty rows instead of the twelve that were moving.

    The fixture is the smallest one that can tell the two apart: a pool of
    **two**, and three jobs of which the middle one returns at once. `slow`
    and `late` are the two ends of a rendezvous, so `late` -- which cannot have
    been claimed until `quick` freed a slot -- has to have been claimed *while
    `slow` was still in flight*. Overlap between those two windows is the
    assertion; the count of jobs run is not, because three jobs run under a
    sequential loop too.
    """
    fixture = _Fixture(concurrency=2)
    rendezvous = _Rendezvous(2)
    windows: dict[str, ClaimWindow] = {}

    async def _handle(job: Job) -> None:
        started = time.perf_counter()
        if job.key != "quick":
            await rendezvous.arrive()
        windows[job.key] = ClaimWindow(
            keys=(job.key,), started_at=started, finished_at=time.perf_counter()
        )

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("slow", "quick", "late")

    assert await fixture.worker.run_once() == 3
    assert set(windows) == {"slow", "quick", "late"}, f"the premise: all three ran -- {windows}"
    assert overlapping([windows["slow"], windows["late"]]), (
        "the top-up claim waited for the whole batch to finish: "
        f"slow={windows['slow']} late={windows['late']}"
    )


async def test_one_jobs_events_are_not_discarded_by_another_jobs_failure(
    fixture: _Fixture,
) -> None:
    """The buffer is per job, and under concurrency that has to be structural.

    One `DeferredEventPublisher` for the life of the worker, with `_run`'s
    `finally` calling `discard()` on it, loses the surviving job's frames to
    the failing job's discard -- an enriched title no client is ever told
    about, with nothing anywhere saying so. The bug is unreachable with one job
    in flight, so the ordering below is what makes it observable: the surviving
    job publishes, waits for the doomed one to have failed and discarded, and
    only then completes.
    """
    failed = asyncio.Event()

    async def _handle(job: Job) -> None:
        await fixture.publish_for(job)
        if job.key == "doomed":
            failed.set()
            raise PortUnavailable("upstream is down")
        # Bounded, never a bare `wait`: against a worker that runs its jobs one
        # at a time this must give up and let the case finish rather than hang.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(failed.wait(), 0.5)

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("kept", "doomed")

    await fixture.worker.run_once()

    assert len(fixture.raised) == 2, "the premise: both jobs raised a frame"
    offered = [event.data.get("job") for event in fixture.bus.offered]
    assert offered == ["kept"], f"the surviving job's frame was lost: {offered}"


# -- recovery: a lease, and the arm that makes it one ------------------------


async def test_recover_requeues_a_claim_older_than_the_lease(fixture: _Fixture) -> None:
    """PRD 08: recovery requeues anything left `in_progress` by an unclean shutdown.

    Without it a killed worker's claims are invisible until a human notices the
    queue has stopped moving.
    """
    await fixture.given("t1")
    await fixture.queue.claim([JobKind.ENRICH])
    fixture.queue.backdate(seconds=DEFAULT_LEASE_SECONDS + 1)

    assert await fixture.worker.recover() == 1
    assert await fixture.worker.run_once() == 1


async def test_recover_leaves_a_claim_that_is_still_being_worked_on(fixture: _Fixture) -> None:
    """The arm with teeth, and the one `older_than_seconds=0.0` fails.

    "An abandoned claim comes back" is satisfied by requeueing *everything*
    running, which is a dead end at more than one worker: there is then no way
    to recover one worker's orphans without corrupting another's. So the
    property pinned here is the negative one -- a claim younger than the lease
    is left alone, however many times recovery runs. The premise is asserted
    first: without a genuinely `running` row to leave alone, `recover() == 0`
    is what an empty queue answers too.
    """
    await fixture.given("live")
    claimed = await fixture.queue.claim([JobKind.ENRICH])
    assert len(claimed) == 1, "the premise: there is a live claim to steal"

    assert await fixture.worker.recover() == 0
    assert await fixture.worker.recover() == 0, "a second pass took it"
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0, (
        "the claim was returned to the queue while its worker still held it"
    )


async def test_a_heartbeat_keeps_a_long_job_out_of_recovery(fixture: _Fixture) -> None:
    """The half that makes a *short* lease safe for a long job.

    Without `touch`, the lease has to exceed the longest job a deployment can
    run -- a `bootstrap` phase can take hours -- so the orphan window becomes
    hours and the recovery is useless in practice. With it the lease is a bound
    on *the process still being alive*.

    Driven through the queue rather than through `JobWorker._heartbeat`,
    because the beat's interval is a third of a 300 s lease and a case that
    waited for one would be a case that waits 100 seconds. What is asserted is
    the property the beat depends on: a touched claim is not recoverable, and
    an untouched one is.
    """
    await fixture.given("long", "abandoned")
    claimed = await fixture.queue.claim([JobKind.ENRICH], limit=2)
    assert len(claimed) == 2, "the premise: two live claims"
    fixture.queue.backdate(seconds=DEFAULT_LEASE_SECONDS + 1)

    still_working_on = next(job for job in claimed if job.key == "long")
    assert await fixture.queue.touch([still_working_on.id]) == 1

    assert await fixture.worker.recover() == 1, "the beat did not protect the job it named"
    assert [job.key for job in await fixture.queue.claim([JobKind.ENRICH], limit=2)] == [
        "abandoned"
    ]


async def test_recover_commits_what_it_requeued(fixture: _Fixture) -> None:
    """A requeue that is never committed is a requeue that did not happen.

    And the process that would have noticed has just started.
    """
    await fixture.given("t1")
    await fixture.queue.claim([JobKind.ENRICH])
    fixture.queue.backdate(seconds=DEFAULT_LEASE_SECONDS + 1)
    await fixture.worker.recover()
    assert fixture.log == ["commit"]


async def test_recover_on_a_clean_queue_requeues_nothing(fixture: _Fixture) -> None:
    await fixture.given("t1")
    assert await fixture.worker.recover() == 0
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1


# -- telemetry --------------------------------------------------------------


async def test_a_workers_span_links_to_whatever_enqueued_the_job(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """PRD 10: "why did the title I just opened take 45 seconds" is one query.

    The enqueue happens inside a request's span and the execution happens minutes later
    in a worker; a `Link` is what joins them.

    A *link*, not a parent: the request has already returned, and a child
    span of a finished parent misstates causality -- and would make the
    request's own trace grow a branch minutes after it ended. The trace-id
    assertion is the one with teeth: a worker that started its span with
    `context=extract(...)` produces the same link-shaped joinability and
    quietly moves the job into the request's trace.
    """
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("server") as server:
        expected = server.get_span_context().trace_id
        await fixture.given("t1", traceparent=current_traceparent())
    await fixture.worker.run_once()
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert job_spans, [span.name for span in spans.get_finished_spans()]
    assert job_spans[0].parent is None, "the worker's span is a root, not a child"
    assert job_spans[0].context is not None
    assert job_spans[0].context.trace_id != expected, "the job joined the request's own trace"
    assert [link.context.trace_id for link in job_spans[0].links] == [expected]


async def test_a_job_enqueued_outside_a_span_gets_an_unlinked_root(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """Everything a background sweep enqueues has a null `traceparent`.

    A worker that invented a link to the all-zero context would join every one of them
    into a trace that never existed.
    """
    await fixture.given("t1")
    await fixture.worker.run_once()
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert job_spans
    assert job_spans[0].links == ()


async def test_a_malformed_traceparent_does_not_fail_the_job(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """A job is not worth failing over its own telemetry.

    The column is free text written by whatever enqueued the work, and a truncated or
    hand-edited value must cost a link rather than a park.
    """
    await fixture.given("t1", traceparent="not-a-traceparent")
    assert await fixture.worker.run_once() == 1
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert job_spans
    assert job_spans[0].links == ()
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0


def test_a_link_is_never_built_from_a_context_that_names_nothing() -> None:
    """`_links_for`'s `is_valid` guard, tested directly because the SDK hides it.

    A `Link` to the all-zero span context is dropped on the way into the span,
    so a worker that built one anyway records the same empty `links` tuple and
    every case above still passes. "No link" and "a link to a trace that never
    existed" are different claims, and the second is what reaches an exporter
    less forgiving than this SDK.
    """
    kinds = JobKind.ENRICH
    assert _links_for(Job(kind=kinds, key="t1")) == []
    assert _links_for(Job(kind=kinds, key="t1", traceparent="not-a-traceparent")) == []
    assert (
        _links_for(
            Job(
                kind=kinds,
                key="t1",
                traceparent="00-d14524c7eba73194c64d589cdd69488a-770641a119523a53-01",
            )
        )[0].context.trace_id
        == 0xD14524C7EBA73194C64D589CDD69488A
    )


async def test_the_span_carries_what_an_operator_would_filter_on(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """PRD 10 reads job outcomes off spans as well as off the table.

    Without the attempt count on the span, "which jobs are on their fourth try" is only
    answerable by querying the queue, which a trace view cannot do.
    """
    fixture.register(JobKind.ENRICH, fixture.raising(PortUnavailable("down")))
    await fixture.given("t1")
    await fixture.worker.run_once()
    await fixture.queue.clear_backoff()
    await fixture.worker.run_once()
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert len(job_spans) == 2
    assert job_spans[1].attributes is not None
    assert job_spans[1].attributes["usher.job.kind"] == "enrich"
    assert job_spans[1].attributes["usher.job.key"] == "t1"
    assert job_spans[1].attributes["usher.job.attempts"] == 1


# -- what a fake cannot say -------------------------------------------------


def test_the_service_never_imports_a_storage_or_transport_library() -> None:
    """PRD 01's layering rule, at module level.

    `import-linter` already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses: a worker reaching for `sqlalchemy` to commit, or
    for `httpx` to decide whether a failure is retryable, instead of for
    `usher.ports.errors`.
    """
    import usher.services.jobs as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


def test_no_temporary_marker_survives_in_this_module() -> None:
    """A temporary marker that outlives its commit is worse than one never written.

    This assertion is what stops a revert re-introducing a marker while the
    state it advertised is gone, which is the contradiction nobody re-reads a
    docstring to notice. Scanned over the whole module rather than one
    docstring: a marker moved to a neighbouring method is the same defect, and
    a scan pointed at one surface reads as coverage.
    """
    import usher.services.jobs as module

    assert "🔴" not in inspect.getsource(module)


def test_the_worker_holds_no_reference_to_a_job_id_it_did_not_claim() -> None:
    """A guard against the shape this loop could drift into.

    `complete` and `fail` take a `uuid.UUID`, and the only ids in scope are the claimed
    jobs'.

    Checked as a signature rather than as behaviour because the failure it prevents --
    completing a neighbouring job -- is unreachable while that stays true.
    """
    import inspect

    from usher.ports.jobs import JobQueue

    assert inspect.signature(JobQueue.complete).parameters["job_id"].annotation is uuid.UUID
    assert inspect.signature(JobQueue.fail).parameters["job_id"].annotation is uuid.UUID


async def test_a_job_waiting_at_its_kinds_ceiling_is_heartbeated_too() -> None:
    """Claimed and not yet settled is what "in flight" has to mean.

    The other spelling loses a job to a duplicate run. A claim is committed the
    instant it is made, so a job queued behind its kind's ceiling is `running`
    in the table while it waits its turn, and the wait can outlast the lease.
    Heartbeated only once it starts, that job ages out and another worker takes
    a claim this one still intends to run. Asserted on the id set the heartbeat
    sends rather than on a timing: **both** ids have to be in the set while
    only one of them is executing.
    """
    fixture = _Fixture(concurrency=2)
    fixture.worker._concurrency[JobKind.ENRICH] = 1
    fixture.worker._gates[JobKind.ENRICH] = asyncio.Semaphore(1)
    entered = 0
    held: set[uuid.UUID] = set()

    async def _handle(job: Job) -> None:
        nonlocal entered, held
        entered += 1
        if entered > 1:
            return
        # Read once, from the job that holds the gate, and only after the other
        # has had a turn of the loop to reach it: `held.update(...)` on every
        # call unions the two jobs' own ids, and reading immediately on entry
        # snapshots before the second task has been scheduled.
        for _ in range(100):
            await asyncio.sleep(0)
            if len(fixture.worker._in_flight) > 1:
                break
        held = set(fixture.worker._in_flight)

    fixture.register(JobKind.ENRICH, _handle)
    await fixture.given("running", "waiting")

    assert await fixture.worker.run_once() == 2
    assert entered == 2, "the premise: both jobs reached the handler"
    assert len(held) == 2, (
        "the job queued behind the ceiling was not heartbeated, so its claim ages out "
        f"of the lease while a worker still intends to run it: {held}"
    )
