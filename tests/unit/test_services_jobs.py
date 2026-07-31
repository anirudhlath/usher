"""The worker loop, against `FakeJobQueue`.

**What this file cannot say, stated before the cases that can.** The fake is
one dict behind one event loop: it has no row lock, no transaction, and no
second session, so "the claim is committed before the work starts" is
checked here as an *ordering of calls* and there is nothing here that could
tell an ordering from a durability. `tests/integration/test_services_jobs.py`
is where a second Postgres backend looks at the queue from outside a running
handler and sees `running` rather than `pending`, and where two real workers
claim disjoint halves of one batch. The fake's own module docstring lists
`SKIP LOCKED` first among the things it cannot express, and the worker is
the code that depends on it most.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.job_queue import FakeJobQueue
from usher.domain.jobs import Job, JobKind, JobPriority, JobStatus
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.jobs import JobRequest
from usher.services.jobs import JobWorker, _links_for
from usher.telemetry import current_traceparent


class _Fixture:
    """Worker, queue, and one event log recording what happened in order.

    The log, rather than two counters: every ordering property below --
    commit before the first handler, a commit after each completion, a
    commit after a failure -- is a statement about *sequence*, and a pair of
    totals cannot distinguish "committed, then ran" from "ran, then
    committed".
    """

    def __init__(self, *, batch_size: int = 20, max_attempts: int = 5) -> None:
        self.queue = FakeJobQueue(max_attempts=max_attempts, backoff_seconds=1.0)
        self.log: list[str] = []
        self.handled: list[Job] = []
        self.worker = JobWorker(queue=self.queue, commit=self._commit, batch_size=batch_size)
        self.worker.register(JobKind.ENRICH, self._handle)

    async def _commit(self) -> None:
        self.log.append("commit")

    async def _handle(self, job: Job) -> None:
        self.log.append(f"handle:{job.key}")
        self.handled.append(job)

    def raising(self, exc: BaseException) -> Callable[[Job], Awaitable[None]]:
        async def _handler(job: Job) -> None:
            self.log.append(f"handle:{job.key}")
            self.handled.append(job)
            raise exc

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
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


# -- the happy path ---------------------------------------------------------


async def test_a_handler_runs_and_the_job_is_removed(fixture: _Fixture) -> None:
    """`startup()` is the assertion with teeth, and `depth()` is not.

    `depth` counts `pending` only, so a worker that ran the handler and never
    called `complete` leaves the row `running` and reads back as an empty
    queue -- measured: deleting the `complete` call fails nothing else in
    this file. `requeue_running` is what can see it, and a job stuck
    `running` forever is the state PRD 08's startup recovery exists to
    clean up.
    """
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0
    assert await fixture.queue.parked() == []
    assert await fixture.worker.startup() == 0, "the job was left claimed rather than completed"


async def test_the_handler_is_given_the_job_it_was_claimed_for(fixture: _Fixture) -> None:
    """`Job.key` is how every handler finds its work -- a title id for
    `enrich`, an external id for `watch_history` -- so handing over anything
    but the claimed row makes each of them operate on the wrong thing."""
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert [job.key for job in fixture.handled] == ["t1"]
    assert fixture.handled[0].status is JobStatus.RUNNING


async def test_an_empty_queue_is_not_an_error(fixture: _Fixture) -> None:
    """A polling worker spends most of its life here."""
    assert await fixture.worker.run_once() == 0
    assert fixture.handled == []


async def test_the_claim_is_committed_before_the_work_starts(fixture: _Fixture) -> None:
    """A worker that claims and works in one uncommitted transaction holds
    every claimed row's lock for the length of the batch, and leaves a
    process killed mid-job with no record that anything was ever tried --
    the claim rolls back and `requeue_running` has nothing to recover.

    An ordering assertion, and only that: this fake has no transaction, so
    nothing here can tell a commit that happened from one that mattered.
    `tests/integration/test_services_jobs.py` looks at the row from a second
    Postgres backend while the handler is still running.
    """
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert fixture.commits_before_the_first_handler >= 1
    assert fixture.log[0] == "commit"


async def test_each_job_is_committed_as_it_finishes(fixture: _Fixture) -> None:
    """Per job, not per batch. A crash nineteen jobs into twenty must not
    re-run the nineteen -- redelivery is safe by construction, but paying for
    it when the alternative is free is not."""
    await fixture.given("t1", "t2", "t3")
    await fixture.worker.run_once()
    assert fixture.log == [
        "commit",
        "handle:t1",
        "commit",
        "handle:t2",
        "commit",
        "handle:t3",
        "commit",
    ]


async def test_the_worker_only_claims_kinds_it_can_handle(fixture: _Fixture) -> None:
    """A worker that claimed every kind would take work it cannot run and
    then either crash on the handler lookup or park it -- and a job parked
    for being offered to the wrong process needs a human to release it."""
    await fixture.given("t1")
    await fixture.given("m1", kind=JobKind.MATCH)
    assert await fixture.worker.run_once() == 1
    depth = await fixture.queue.depth()
    assert depth[JobKind.MATCH] == 1, "the worker claimed work it has no handler for"
    assert depth[JobKind.ENRICH] == 0


# -- failure, which is the point --------------------------------------------


async def test_a_transient_failure_backs_the_job_off(fixture: _Fixture) -> None:
    fixture.worker.register(JobKind.ENRICH, fixture.raising(PortUnavailable("upstream is down")))
    await fixture.given("t1")
    await fixture.worker.run_once()
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1
    assert await fixture.queue.parked() == []


async def test_a_backed_off_job_is_not_immediately_re_claimed(fixture: _Fixture) -> None:
    """The hot loop the backoff exists to prevent, at the worker level: one
    broken upstream must not become a request per handler invocation for as
    long as it stays broken."""
    fixture.worker.register(JobKind.ENRICH, fixture.raising(PortUnavailable("upstream is down")))
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert await fixture.worker.run_once() == 0, "the failed job was re-claimed with no wait"


async def test_malformed_data_parks_immediately(fixture: _Fixture) -> None:
    """`PortDataMalformed`: "the upstream answered, and the answer was wrong.
    Retrying does not help, so a caller parks the work rather than backing
    off." Five identical failures and a five-times-longer wait before a human
    sees it is the alternative."""
    fixture.worker.register(
        JobKind.ENRICH, fixture.raising(PortDataMalformed("TMDb returned a list"))
    )
    await fixture.given("t1")
    await fixture.worker.run_once()
    parked = await fixture.queue.parked()
    assert [job.key for job in parked] == ["t1"]
    assert parked[0].attempts == 1, "parked at the ceiling rather than immediately"


async def test_a_job_that_keeps_failing_is_parked_rather_than_retried_forever(
    fixture: _Fixture,
) -> None:
    """PRD 08: "after N attempts a job is *parked* with its error, not
    retried forever and not silently dropped." All three outcomes are
    asserted: it stopped being claimable, it is listed, and it kept its
    error."""
    fixture.worker.register(JobKind.ENRICH, fixture.raising(PortUnavailable("still down")))
    await fixture.given("t1")
    for _ in range(5):
        await fixture.worker.run_once()
        await fixture.queue.clear_backoff()
    parked = await fixture.queue.parked()
    assert [(job.key, job.attempts) for job in parked] == [("t1", 5)]
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0
    assert await fixture.worker.run_once() == 0, "a parked job was claimed again"


async def test_a_parked_job_keeps_the_error_that_parked_it(fixture: _Fixture) -> None:
    """The whole of "not silently dropped" is this string: it is what an
    operator reads in the admin list, and `str(exc)` rather than the
    exception object because PRD 08's credentials-never-logged rule applies
    to a column as much as to a log line."""
    fixture.worker.register(
        JobKind.ENRICH, fixture.raising(PortDataMalformed("TMDb returned a list"))
    )
    await fixture.given("t1")
    await fixture.worker.run_once()
    parked = await fixture.queue.parked()
    assert parked[0].last_error is not None
    assert "TMDb returned a list" in parked[0].last_error


async def test_a_failure_costs_its_own_job_and_not_the_batch(fixture: _Fixture) -> None:
    """One poisoned job in a claimed batch of three must not abandon the
    other two -- at `batch_size=20` against a queue the size of this
    library, a try/except outside the loop turns one bad payload into
    nineteen jobs silently returned to `pending` on every pass."""
    failing = fixture.raising(PortDataMalformed("bad payload"))

    async def _handle(job: Job) -> None:
        if job.key == "t2":
            await failing(job)
            return
        fixture.log.append(f"handle:{job.key}")
        fixture.handled.append(job)

    fixture.worker.register(JobKind.ENRICH, _handle)
    await fixture.given("t1", "t2", "t3")
    assert await fixture.worker.run_once() == 3
    assert [job.key for job in fixture.handled] == ["t1", "t2", "t3"]
    assert [job.key for job in await fixture.queue.parked()] == ["t2"]
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0


async def test_a_bug_in_a_handler_is_not_recorded_as_an_upstream_failure(
    fixture: _Fixture,
) -> None:
    """A `ZeroDivisionError` is not a `UsherPortError` and must propagate.
    Swallowing it into `fail(retryable=True)` turns a crash into a job that
    retries five times and then parks with a misleading error -- and the
    worker keeps running, so nothing is ever loud."""
    fixture.worker.register(JobKind.ENRICH, fixture.raising(ZeroDivisionError("bug")))
    await fixture.given("t1")
    with pytest.raises(ZeroDivisionError):
        await fixture.worker.run_once()
    assert await fixture.queue.parked() == []


async def test_every_port_error_backs_off_rather_than_escaping(fixture: _Fixture) -> None:
    """Not just the two subclasses the other cases happen to raise. A worker
    that named `PortUnavailable` specifically would let `PortAuthFailed` and
    `PortRateLimited` escape `run_once` and kill the loop -- which is the
    same failure `ReconcileService` guards against one lane over."""

    class _Boom(UsherPortError):
        pass

    fixture.worker.register(JobKind.ENRICH, fixture.raising(_Boom("something at the edge")))
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1


async def test_a_claim_requeued_out_from_under_the_worker_does_not_crash(
    fixture: _Fixture,
) -> None:
    """`fail` answers `None` for an id the queue no longer knows -- a restart
    requeued the claim and someone else finished it. The worker has nothing
    useful to do with that news except not crash on it, and crashing would
    take the whole loop down over a job that has already succeeded."""

    async def _steal_then_fail(job: Job) -> None:
        await fixture.queue.complete(job.id)
        raise PortUnavailable("upstream is down")

    fixture.worker.register(JobKind.ENRICH, _steal_then_fail)
    await fixture.given("t1")
    assert await fixture.worker.run_once() == 1
    assert await fixture.queue.parked() == []


# -- startup ----------------------------------------------------------------


async def test_startup_requeues_jobs_left_running(fixture: _Fixture) -> None:
    """PRD 08: "Startup requeues anything left `in_progress` by an unclean
    shutdown." Without it a killed worker's claims are invisible until a
    human notices the queue has stopped moving."""
    await fixture.given("t1")
    await fixture.queue.claim([JobKind.ENRICH])
    assert await fixture.worker.startup() == 1
    assert await fixture.worker.run_once() == 1


async def test_startup_commits_what_it_requeued(fixture: _Fixture) -> None:
    """A requeue that is never committed is a requeue that did not happen,
    and the process that would have noticed has just started."""
    await fixture.given("t1")
    await fixture.queue.claim([JobKind.ENRICH])
    await fixture.worker.startup()
    assert fixture.log == ["commit"]


async def test_startup_on_a_clean_queue_requeues_nothing(fixture: _Fixture) -> None:
    await fixture.given("t1")
    assert await fixture.worker.startup() == 0
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 1


# -- telemetry --------------------------------------------------------------


async def test_a_workers_span_links_to_whatever_enqueued_the_job(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """PRD 10: "why did the title I just opened take 45 seconds" is one
    query. The enqueue happens inside a request's span and the execution
    happens minutes later in a worker; a `Link` is what joins them.

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
    """Everything a background sweep enqueues has a null `traceparent`. A
    worker that invented a link to the all-zero context would join every one
    of them into a trace that never existed."""
    await fixture.given("t1")
    await fixture.worker.run_once()
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert job_spans
    assert job_spans[0].links == ()


async def test_a_malformed_traceparent_does_not_fail_the_job(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """A job is not worth failing over its own telemetry. The column is free
    text written by whatever enqueued the work, and a truncated or
    hand-edited value must cost a link rather than a park."""
    await fixture.given("t1", traceparent="not-a-traceparent")
    assert await fixture.worker.run_once() == 1
    job_spans = [span for span in spans.get_finished_spans() if span.name == "job.enrich"]
    assert job_spans
    assert job_spans[0].links == ()
    assert (await fixture.queue.depth())[JobKind.ENRICH] == 0


def test_a_link_is_never_built_from_a_context_that_names_nothing() -> None:
    """`_links_for`'s `is_valid` guard, tested directly because the SDK hides
    it: a `Link` to the all-zero span context is dropped on the way into the
    span, so a worker that built one anyway records the same empty `links`
    tuple and every case above still passes. Measured -- deleting the guard
    survived the whole file.

    Kept rather than deleted, because "no link" and "a link to a trace that
    never existed" are different claims, and the second one is what reaches
    an exporter that is less forgiving than this SDK.
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
    """PRD 10 reads job outcomes off spans as well as off the table. Without
    the attempt count on the span, "which jobs are on their fourth try" is
    only answerable by querying the queue, which a trace view cannot do."""
    fixture.worker.register(JobKind.ENRICH, fixture.raising(PortUnavailable("down")))
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
    """ADR-0009 and PRD 01's layering rule, at module level. `import-linter`
    already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses: a worker reaching for `sqlalchemy` to
    commit, or for `httpx` to decide whether a failure is retryable, instead
    of for `usher.ports.errors`."""
    import usher.services.jobs as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


def test_the_worker_holds_no_reference_to_a_job_id_it_did_not_claim() -> None:
    """A guard against the shape this loop could drift into: `complete` and
    `fail` take a `uuid.UUID`, and the only ids in scope are the claimed
    jobs'. Checked as a signature rather than as behaviour because the
    failure it prevents -- completing a neighbouring job -- is unreachable
    while that stays true."""
    import inspect

    from usher.ports.jobs import JobQueue

    assert inspect.signature(JobQueue.complete).parameters["job_id"].annotation is uuid.UUID
    assert inspect.signature(JobQueue.fail).parameters["job_id"].annotation is uuid.UUID
