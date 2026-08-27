"""The scheduler loop, over fake jobs and an injected clock.

**Every job in this file is a fake, and that is the point rather than a
convenience.** J4 ships `Scheduler` with an *empty* registry: the two
registrations are separate tasks (`search_queries` retention and the neighbour
rebuild), so a case here that drove a real job would be testing a component
this commit does not contain. What the cases below pin is the loop -- the due
comparison, the sequencing, the failure isolation, the empty-registry state and
the two lifecycle promises -- against jobs that exist only to be observed.

**And the empty registry is asserted rather than merely shipped.** An empty
registry that nothing checks is indistinguishable from one somebody forgot to
fill, which is why `test_the_registry_a_composition_root_builds_ships_empty`
exists and why it names both halves: `build_scheduler` returns a `Scheduler`,
and that scheduler has no jobs.

**The clock's origin is deliberately not zero.** `.claude/rules/
testing-discipline.md`: *"a fixture whose origin is the identity element of the
operation under test cannot distinguish the operation from its absence"* -- a
`datetime` at the epoch would make `now - last` and `now` the same instant for
a `last_done()` of `datetime.min`, and the whole subject here is a subtraction.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from usher.composition import build_scheduler
from usher.config import Settings
from usher.ports.scheduler import ScheduledJob
from usher.services.scheduler import Scheduler

# Not the epoch, and not a round number either -- see the module docstring.
_NOW = datetime(2026, 8, 27, 18, 30, 43, tzinfo=UTC)
_HOUR = timedelta(hours=1)


class _Clock:
    """A clock a case moves by hand, so no case waits on a real one."""

    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now

    def read(self) -> datetime:
        return self.now


class _Fake(ScheduledJob):
    """A job that records what was asked of it.

    `name` and `period` are properties because the port declares them
    abstract, which is the half of ADR-0001 a bare annotation would give up:
    an implementation that forgets one must fail at instantiation rather than
    at the first metric label.
    """

    def __init__(
        self,
        name: str,
        *,
        period: timedelta = _HOUR,
        last: datetime | None = None,
        fails: bool = False,
        last_done_fails: bool = False,
        blocks: asyncio.Event | None = None,
    ) -> None:
        self._name = name
        self._period = period
        self._last = last
        self._fails = fails
        self._last_done_fails = last_done_fails
        self._blocks = blocks
        self.runs = 0
        self.asked = 0
        # Wall-clock intervals, one per run, so a case can assert two runs did
        # not overlap. A count of two completions is what a concurrent pair
        # produces too -- CLAUDE.md's fourth evidence rule, applied in the
        # direction that wants serialisation.
        self.windows: list[tuple[float, float]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def period(self) -> timedelta:
        return self._period

    async def last_done(self) -> datetime | None:
        self.asked += 1
        if self._last_done_fails:
            raise RuntimeError("the artefact could not be read")
        return self._last

    async def run(self) -> None:
        started = asyncio.get_running_loop().time()
        self.runs += 1
        try:
            if self._blocks is not None:
                await self._blocks.wait()
            else:
                # One real suspension, so two concurrent runs would genuinely
                # interleave here. Without it a `gather` would still produce
                # disjoint windows and the sequencing case could not fail.
                await asyncio.sleep(0.01)
            if self._fails:
                raise ZeroDivisionError("the scheduled job blew up")
        finally:
            self.windows.append((started, asyncio.get_running_loop().time()))


def _scheduler(*jobs: ScheduledJob, clock: _Clock | None = None) -> Scheduler:
    scheduler = Scheduler(tick_seconds=60.0, now=(clock or _Clock()).read)
    for job in jobs:
        scheduler.register(job)
    return scheduler


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()


@pytest.fixture
def lines() -> Iterator[list[str]]:
    captured: list[str] = []
    sink = logger.add(captured.append, level="DEBUG", format="{level.name}|{message}")
    yield captured
    logger.remove(sink)


# -- the due comparison ----------------------------------------------------


async def test_a_job_whose_period_has_not_elapsed_is_not_run() -> None:
    """**The failing test this task was written against**, and its positive
    control is the first arm.

    A scheduler that ran nothing at all also satisfies *"the not-due job did
    not run"*, so the case asserts the due job **did**, and both assertions
    carry their own message. One tick, two jobs on one registry, one clock.
    """
    clock = _Clock()
    due = _Fake("due", period=_HOUR, last=clock.now - timedelta(hours=2))
    waiting = _Fake("waiting", period=_HOUR, last=clock.now - timedelta(minutes=30))
    scheduler = _scheduler(due, waiting, clock=clock)

    ran = await scheduler.tick()

    assert due.runs == 1, "the job whose period had elapsed was not run"
    assert waiting.runs == 0, "a job whose period has not elapsed was run anyway"
    assert ran == 1


async def test_a_job_at_exactly_its_period_is_due() -> None:
    """The boundary, because `>=` and `>` are the two spellings and a fixture
    an hour either side of it cannot tell them apart.

    A period is *"at least this long since the last completion"*, so the
    instant it has been exactly that long the job is due.
    """
    clock = _Clock()
    job = _Fake("exact", period=_HOUR, last=clock.now - _HOUR)
    assert await _scheduler(job, clock=clock).tick() == 1
    assert job.runs == 1


async def test_a_job_that_has_never_run_is_due() -> None:
    """*"Never built"* and *"not due"* are the two states a naive
    `now - last_done > period` collapses -- with a `TypeError`, not a wrong
    answer, because `datetime - None` does not subtract.

    The neighbour rebuild on a fresh deployment is exactly this state, so it
    is the first one a registration will meet.
    """
    job = _Fake("never", last=None)
    assert await _scheduler(job).tick() == 1
    assert job.runs == 1


# -- the empty registry ----------------------------------------------------


def test_the_registry_a_composition_root_builds_ships_empty() -> None:
    """**J4 ships the loop and no registrations**, and an empty registry that
    nothing asserts is indistinguishable from one somebody forgot to fill.

    Both halves are named: the composition root returns a `Scheduler`, and
    that scheduler's registry is empty. J5 and J6 each add one entry here and
    this assertion is what makes each of them a visible decision rather than a
    line nobody reads.
    """
    scheduler = build_scheduler(
        Settings(
            database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
            secret_key="0" * 32,
        )
    )
    assert isinstance(scheduler, Scheduler)
    assert scheduler.jobs == (), f"the registry shipped with {[j.name for j in scheduler.jobs]}"


async def test_an_empty_registry_logs_once_over_three_ticks(lines: list[str]) -> None:
    """A scheduler that spammed a line every tick forever is the shape an
    operator mutes -- and then never sees the real one.

    Three ticks, one line. Asserting after a single tick cannot tell *"once"*
    from *"per tick"*, which is the same shape
    `test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass`
    needed.
    """
    scheduler = _scheduler()

    for _ in range(3):
        assert await scheduler.tick() == 0

    empty = [line for line in lines if "no registered jobs" in line]
    assert len(empty) == 1, f"expected one line about the empty registry, got {empty}"


async def test_registering_a_job_ends_the_empty_state(lines: list[str]) -> None:
    """The control for the case above: *"one line over three ticks"* is also
    what a scheduler that logged nothing at all produces, and it is also what
    one that never notices a registration produces."""
    scheduler = _scheduler()
    await scheduler.tick()
    assert [line for line in lines if "no registered jobs" in line]

    job = _Fake("late", last=None)
    scheduler.register(job)
    assert await scheduler.tick() == 1
    assert job.runs == 1


def test_two_jobs_may_not_share_a_name() -> None:
    """`name` is a metric label and a span name, so two jobs under one name
    make `usher.scheduler.job.duration` a histogram over two populations with
    nothing saying so."""
    scheduler = _scheduler(_Fake("twin"))
    with pytest.raises(ValueError, match="twin"):
        scheduler.register(_Fake("twin"))


# -- sequencing ------------------------------------------------------------


def _overlap(one: tuple[float, float], other: tuple[float, float]) -> float:
    return min(one[1], other[1]) - max(one[0], other[0])


async def test_two_due_jobs_run_one_at_a_time() -> None:
    """**Asserted by observed non-overlap, never by a count.** Two completions
    is exactly what a concurrent pair produces, so the fakes record the
    wall-clock interval each occupied and the case asserts the two do not
    intersect.

    ADR-0037's argument for `asyncio.wait` over a `TaskGroup` applies here
    unchanged and one abstraction lower: a group cancels its siblings on the
    first escape, which turns one poisoned job into a three-hour rebuild
    abandoned mid-page. And one task per job is what makes *"two hours-long
    jobs contending for the same pool"* reachable without a bound.
    """
    clock = _Clock()
    first = _Fake("first", last=clock.now - timedelta(hours=2))
    second = _Fake("second", last=clock.now - timedelta(hours=2))

    assert await _scheduler(first, second, clock=clock).tick() == 2

    assert len(first.windows) == 1 and len(second.windows) == 1, (
        "two windows are a statement about two runs; one is a statement about nothing"
    )
    assert _overlap(first.windows[0], second.windows[0]) <= 0.0, (
        f"the two jobs overlapped: {first.windows[0]} and {second.windows[0]}"
    )


# -- failure isolation -----------------------------------------------------


async def test_a_failing_job_does_not_stop_its_siblings(lines: list[str]) -> None:
    """Without the named `except Exception` the tick's first raise takes the
    rest of the registry with it, and the loop task dies -- at which point
    CPython reports the unretrieved exception at GC time, to stderr, with no
    job name in it. That is the shape `LaneSupervisor._guard` exists for.

    The log line has to name the job, or an operator has an exception and no
    idea which of two batches raised it.
    """
    clock = _Clock()
    poison = _Fake("poison", last=None, fails=True)
    healthy = _Fake("healthy", last=None)

    ran = await _scheduler(poison, healthy, clock=clock).tick()

    assert poison.runs == 1
    assert healthy.runs == 1, "a failing job took its sibling down with it"
    assert ran == 1, "a job that raised was counted as having run"
    failures = [line for line in lines if "poison" in line]
    assert failures, f"the failure was not logged with the job's name: {lines}"
    assert "ZeroDivisionError" in "\n".join(failures)


async def test_a_last_done_that_raises_neither_runs_the_job_nor_stops_the_tick(
    lines: list[str],
) -> None:
    """`last_done()` reads an artefact, so it is a database call and fails the
    way every database call fails. Running the job anyway would start a
    multi-hour rebuild on the strength of a read that did not answer."""
    clock = _Clock()
    unreadable = _Fake("unreadable", last=None, last_done_fails=True)
    healthy = _Fake("healthy", last=None)

    assert await _scheduler(unreadable, healthy, clock=clock).tick() == 1

    assert unreadable.runs == 0, "a job ran on the strength of a read that raised"
    assert healthy.runs == 1
    assert [line for line in lines if "unreadable" in line]


async def test_a_failing_job_is_not_offered_again_on_the_very_next_tick() -> None:
    """🔴 **The hole the acceptance criterion above cannot see.**

    *"A failing job does not stop the loop"* is satisfied by a loop that also
    never progresses. With no stored state a **failed** run is
    indistinguishable from one never run, so a job that raises leaves
    `last_done()` exactly where it was, is due again on the next tick, and
    retries at the tick rate forever -- 288 attempts a day at the 300 s
    default, against a database that is by hypothesis already unhappy.

    So a failed run backs the job off, and the second tick is the assertion:
    a scheduler with no backoff runs it twice.
    """
    clock = _Clock()
    job = _Fake("poison", period=_HOUR, last=None, fails=True)
    scheduler = _scheduler(job, clock=clock)

    await scheduler.tick()
    assert job.runs == 1, "the first tick did not run the job, so there is no failure to space"
    await scheduler.tick()

    assert job.runs == 1, "a job that raised was retried on the very next tick"


async def test_the_backoff_expires_and_never_exceeds_the_period() -> None:
    """Two properties in one case, because each is what stops the other from
    being wrong.

    **It expires**, or a single blip retires the job for the life of the
    process -- which is worse than the hot loop it replaces, and silent.

    **It is capped at the job's own period**, which is what makes it a bound
    on the retry *rate* rather than a second schedule: a job that keeps
    failing settles to being offered no more often than the schedule it would
    have had if every attempt had succeeded. The `tick_seconds` here is an
    hour and the period is an hour, so an uncapped doubling would put the
    second retry two hours out and this case would fail.
    """
    clock = _Clock()
    job = _Fake("poison", period=_HOUR, last=None, fails=True)
    scheduler = Scheduler(tick_seconds=_HOUR.total_seconds(), now=clock.read)
    scheduler.register(job)

    await scheduler.tick()
    assert job.runs == 1

    clock.now += timedelta(minutes=59)
    await scheduler.tick()
    assert job.runs == 1, "the backoff expired early"

    clock.now += timedelta(minutes=2)
    await scheduler.tick()
    assert job.runs == 2, "the backoff never expired"

    # And the cap holds on the *second* failure, where an uncapped doubling
    # would ask for two periods.
    clock.now += timedelta(minutes=61)
    await scheduler.tick()
    assert job.runs == 3, "the backoff doubled past the job's own period"


async def test_a_run_that_succeeds_clears_the_backoff() -> None:
    """Otherwise a job that failed once carries the penalty forever, and the
    doubling would go on doubling across successes.

    The premise is the first arm: without a failure to clear there is nothing
    for this case to be about.
    """
    clock = _Clock()
    job = _Fake("flaky", period=_HOUR, last=None, fails=True)
    scheduler = _scheduler(job, clock=clock)

    await scheduler.tick()
    assert job.runs == 1
    job._fails = False
    clock.now += timedelta(minutes=2)
    await scheduler.tick()
    assert job.runs == 2, "the premise: the backoff had expired and the job ran clean"

    # A clean run resets the counter, so the *next* failure is spaced by one
    # tick again rather than by two.
    job._fails = True
    await scheduler.tick()
    assert job.runs == 3
    clock.now += timedelta(seconds=61)
    await scheduler.tick()
    assert job.runs == 4, "a clean run did not reset the doubling"


async def test_a_backed_off_job_is_not_asked_when_it_was_last_done() -> None:
    """The backoff is checked **before** the artefact read, so a job this
    process has already decided not to offer costs no query at all.

    That is the whole point of spacing the retries: a scheduler that still
    issued `last_done()` every tick would have moved the hot loop from the run
    to the read.
    """
    clock = _Clock()
    job = _Fake("poison", period=_HOUR, last=None, fails=True)
    scheduler = _scheduler(job, clock=clock)

    await scheduler.tick()
    asked = job.asked
    await scheduler.tick()

    assert job.asked == asked, "a backed-off job was still asked when it was last done"


async def test_a_cancelled_job_is_re_raised_rather_than_swallowed() -> None:
    """`stop()` works by cancelling, so an `except Exception` that also caught
    `asyncio.CancelledError` would turn a shutdown into a logged failure and a
    loop that carried on."""
    started = asyncio.Event()

    class _Cancels(_Fake):
        async def run(self) -> None:
            self.runs += 1
            started.set()
            await asyncio.sleep(3600)

    job = _Cancels("cancels", last=None)
    scheduler = _scheduler(job)
    tick = asyncio.create_task(scheduler.tick())
    await started.wait()
    tick.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tick


async def test_a_tick_that_raises_does_not_end_the_loop(lines: list[str]) -> None:
    """The loop's own boundary, one layer above the per-job one: a tick that
    failed for a reason no job owns must slow the scheduler down, never end
    it. A loop that returned would leave the deployment with no scheduler and
    nothing saying so until the next restart."""
    ticks = 0

    class _Loop(Scheduler):
        async def tick(self) -> int:
            nonlocal ticks
            ticks += 1
            raise RuntimeError("the tick itself blew up")

    scheduler = _Loop(tick_seconds=0.001)
    await scheduler.start()
    try:
        for _ in range(200):
            if ticks >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        await scheduler.stop()

    assert ticks >= 2, f"the loop stopped after {ticks} tick(s)"
    assert [line for line in lines if "the tick itself blew up" in line]


# -- the lifecycle ---------------------------------------------------------


async def test_start_creates_the_task_and_awaits_nothing() -> None:
    """The M5 supervisor's own draft got this wrong in exactly this way, and
    it is what keeps `/health` answering 200 with Postgres down: a `start()`
    that read anything would turn a database outage into a failure to boot.

    Driven one step by hand -- `coro.send(None)` must raise `StopIteration`
    for a coroutine that never suspended, and hands back a future for one that
    parked. No polling, no clock, no timeout, and it cannot be satisfied by a
    slow-but-eventually-fine implementation.

    The second assertion is what stops this passing because `start()` did
    nothing at all.
    """
    scheduler = _scheduler(_Fake("first", last=None))
    coro = scheduler.start()
    try:
        with pytest.raises(StopIteration):
            coro.send(None)
        assert scheduler.running() is True
    finally:
        coro.close()
        await scheduler.stop()


async def test_the_first_last_done_happens_inside_the_loop_task() -> None:
    """The other half of the promise above: `start()` asking nothing is only
    useful if the loop then asks. Without this, a scheduler that started a
    task doing nothing would satisfy the case above perfectly."""
    job = _Fake("first", last=None)
    scheduler = Scheduler(tick_seconds=0.001)
    scheduler.register(job)
    await scheduler.start()
    try:
        for _ in range(200):
            if job.asked:
                break
            await asyncio.sleep(0.005)
    finally:
        await scheduler.stop()
    assert job.asked >= 1, "the loop task never asked a job when it was last done"


async def test_stop_cancels_an_in_flight_job_and_awaits_its_task() -> None:
    """An in-flight three-and-a-half-hour rebuild is cancelled at its next
    `await`, which is inside a page, and that page's transaction rolls back --
    safe because each page deletes and re-inserts its own seeds' rows in one
    transaction (`services/similar.py`).

    **The task object is asserted `done()`, not `running()` reported false.** A
    `stop()` that merely dropped its reference reports exactly the same thing
    and leaks the task.
    """
    blocked = asyncio.Event()
    job = _Fake("blocked", last=None, blocks=blocked)
    scheduler = _scheduler(job)

    await scheduler.start()
    for _ in range(200):
        if job.runs:
            break
        await asyncio.sleep(0.005)
    task = scheduler.task()
    assert task is not None and not task.done()
    assert job.runs == 1, "the job never started, so there was nothing in flight to cancel"

    await scheduler.stop()

    assert task.done(), "stop() returned with the loop task still live"
    assert not blocked.is_set(), "the case released the job itself, so nothing was cancelled"
    assert scheduler.running() is False


async def test_stop_before_start_is_not_an_error() -> None:
    """`LaneSupervisor.stop()` runs on every shutdown, including one whose
    `start()` was gated off by the setting."""
    await _scheduler().stop()


# -- observability ---------------------------------------------------------


async def test_the_due_gauge_reads_a_snapshot_the_tick_refreshes() -> None:
    """**The gauge may not query the database**, and that is not a style
    preference: OTel invokes an observable callback from the metric reader's
    background thread, every read here is a coroutine on asyncpg, and a
    callback that queried would have to bounce one onto the event loop and
    block the exporter thread on it -- a deadlock whenever the loop is itself
    blocked (`.claude/rules/api-telemetry-and-lanes.md`).

    So `read()` is synchronous and hands back the tick's own reading. Negative
    means not due, which is what makes one series answer *"how overdue"* and
    *"how long left"* without a second instrument.
    """
    clock = _Clock()
    overdue = _Fake("overdue", period=_HOUR, last=clock.now - timedelta(hours=3))
    waiting = _Fake("waiting", period=_HOUR, last=clock.now - timedelta(minutes=15))
    scheduler = _scheduler(overdue, waiting, clock=clock)

    assert scheduler.read() == {}, "a gauge reported before anything had read an artefact"
    await scheduler.tick()

    due = scheduler.read()
    assert due["overdue"] == pytest.approx(timedelta(hours=2).total_seconds())
    assert due["waiting"] == pytest.approx(-timedelta(minutes=45).total_seconds())


async def test_a_job_that_has_never_run_reports_no_due_point_at_all() -> None:
    """*"Seconds since `last_done()` minus period"* has no value when there is
    no `last_done()`, and a fabricated zero would read as *"exactly due"* --
    the same rule `_observations` states as *"no reader means no observation,
    never a zero"*.

    The absence is bounded rather than open-ended: a never-run job is due, so
    the tick runs it and the next tick has a reading.
    """
    job = _Fake("never", last=None)
    scheduler = _scheduler(job)

    await scheduler.tick()

    assert "never" not in scheduler.read(), f"a never-run job reported {scheduler.read()}"


async def test_a_job_whose_last_done_raises_reports_no_due_point() -> None:
    """And the previous reading is dropped rather than left standing: a gauge
    still reporting a number for a job whose artefact cannot be read is the
    stale-but-wrong case the snapshot design exists to avoid."""
    clock = _Clock()
    job = _Fake("flaky", period=_HOUR, last=clock.now - timedelta(hours=3))
    scheduler = _scheduler(job, clock=clock)
    await scheduler.tick()
    assert "flaky" in scheduler.read()

    job._last_done_fails = True
    await scheduler.tick()

    assert "flaky" not in scheduler.read()


async def test_the_job_span_is_a_root_even_when_the_tick_runs_inside_a_span(
    spans: InMemorySpanExporter,
) -> None:
    """**A root span with a `Link`, never a child**, and `context=Context()`
    is what makes "root" structural rather than a property of where the task
    happened to be created.

    `asyncio.create_task` copies the ambient context, so a lifespan or a test
    that started the scheduler inside a span would otherwise make every
    scheduled run a child of one request forever. The link is what keeps the
    causality readable without the parentage.

    The enclosing span is the control: without it a scheduler that dropped
    `Context()` would still produce a parentless span and this case could not
    fail.
    """
    job = _Fake("linked", last=None)
    scheduler = _scheduler(job)

    with trace.get_tracer("test").start_as_current_span("enclosing") as enclosing:
        await scheduler.tick()
        enclosing_context = enclosing.get_span_context()

    finished = {span.name: span for span in spans.get_finished_spans()}
    assert "scheduler.linked" in finished, f"no span named for the job: {sorted(finished)}"
    run = finished["scheduler.linked"]
    assert run.parent is None, "the scheduled run is a child of whatever started the scheduler"
    assert [link.context.span_id for link in run.links] == [enclosing_context.span_id]


# -- the settings ----------------------------------------------------------


def test_the_scheduler_is_off_by_default() -> None:
    """A settings default is a claim like any other. Off, because a fresh
    deployment has no embeddings and nothing excludes a second runner --
    ADR-0046's decision 3, and both halves of it are measured there."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher", secret_key="0" * 32
    )
    assert settings.scheduler_enabled is False
    assert settings.scheduler_tick_seconds == 300.0


def test_the_tick_period_has_a_measured_floor() -> None:
    """`ge=60.0`, and the floor is measured rather than stylistic: a tick is
    ~144 ms of database work for the one job that will exist (ADR-0046's
    evidence table), which is 0.24% of a minute at the floor and 14% of a
    second at one.
    """
    for refused in (0.0, 1.0, 59.9):
        with pytest.raises(ValueError, match="scheduler_tick_seconds"):
            Settings(
                database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
                secret_key="0" * 32,
                scheduler_tick_seconds=refused,
            )
    accepted = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        scheduler_tick_seconds=60.0,
    )
    assert accepted.scheduler_tick_seconds == 60.0
