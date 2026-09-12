"""The scheduler loop over fake jobs, and `SearchQueryRetention` over a fake
store -- both with an injected clock and no database anywhere.

**Every job the *loop* cases drive is a fake, and that is the point rather
than a convenience.** What they pin is the loop -- the due comparison, the
sequencing, the failure isolation and the two lifecycle promises -- against
jobs that exist only to be observed, so a defect in a registration cannot make
one of them green or red.

**The retention cases below are the other half and drive the real job**
(M10's J5) over `FakeSearchQueryRepository` through a recording scope. Its
`last_done()` is an *arithmetic* claim over what the store answers, so a fake
store is the right arm for it; the Postgres arm -- the real statement, the
real boundary and the real commit -- is
`tests/integration/test_search_query_retention.py`.

**The clock's origin is deliberately not zero.** `.claude/rules/
testing-discipline.md`: *"a fixture whose origin is the identity element of the
operation under test cannot distinguish the operation from its absence"* -- a
`datetime` at the epoch would make `now - last` and `now` the same instant for
a `last_done()` of `datetime.min`, and the whole subject here is a subtraction.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.search_query_repository import FakeSearchQueryRepository
from usher.composition import build_scheduler
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.domain.ids import new_id
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.scheduler import ScheduledJob
from usher.ports.search import SearchMode
from usher.services.scheduler import (
    RETENTION_PERIOD,
    Scheduler,
    SearchQueryRetention,
    SearchQueryScope,
)
from usher.services.similar import NeighborRebuildJob

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


def _no_sessions() -> async_sessionmaker[AsyncSession]:
    """A real session factory over a real engine against a port nothing
    listens on.

    `build_engine` opens no connection -- that is `create_app`'s own lifespan
    property -- and `build_scheduler` only closes over this, so no case here
    touches a socket. A `Mock` would satisfy the type and would let a
    `build_scheduler` that *used* the factory eagerly pass silently.
    """
    return build_session_factory(build_engine("postgresql+asyncpg://u:p@127.0.0.1:1/usher"))


class _RecordingScope:
    """A `SearchQueryScope` over one repository, counting how many times it
    was opened and how many of those exits were clean.

    **`opened` is the assertion "a commit per chunk" is made through on this
    arm.** The fake has no transaction, so a commit is not observable as a
    stored effect -- what *is* observable is that `run()` opens a fresh scope
    per chunk rather than one for the whole drain, which is the structure the
    commit hangs off. The Postgres arm asserts the commit itself, on a
    connection that never saw the writing session.
    """

    def __init__(self, repository: SearchQueryRepository) -> None:
        self._repository = repository
        self.opened = 0
        self.closed_cleanly = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SearchQueryRepository]:
        self.opened += 1
        yield self._repository
        self.closed_cleanly += 1


def _scope_over(repository: SearchQueryRepository) -> SearchQueryScope:
    return _RecordingScope(repository)


def _row(*, at: datetime, user_id: uuid.UUID) -> SearchQueryRecord:
    """One `search_queries` row, with everything this file does not vary
    filled in. Invented values, like every fixture here."""
    return SearchQueryRecord(
        id=new_id(),
        at=at,
        user_id=user_id,
        query="the quiet vacuum",
        mode=SearchMode.FULL_TEXT,
        result_count=1,
        latency_ms=1,
    )


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


#: A ceiling on any `SearchQueryRetention.run()` this file drives -- the unit
#: half of `tests/integration/test_search_query_retention.py::DRAIN_DEADLINE`,
#: and it only works because `FakeSearchQueryRepository.prune` awaits.
#: A drain whose terminator is broken has to reach pytest as a failure rather
#: than as a hang; J5 shipped exactly that bug.
DRAIN_DEADLINE = 5.0


async def _drain(job: SearchQueryRetention) -> None:
    """`job.run()`, bounded. See `DRAIN_DEADLINE`."""
    await asyncio.wait_for(job.run(), DRAIN_DEADLINE)


async def _tick(scheduler: Scheduler) -> int:
    """`scheduler.tick()`, bounded. See `DRAIN_DEADLINE`.

    🔴 **`Scheduler.tick()` awaits `job.run()` with no deadline of its own**,
    so bounding the direct `run()` call sites was not enough: a job that never
    returns wedges the tick, and the suite reported a hang rather than a
    failure. That is a property of the shipped component and not of these
    tests -- issue #83 -- and this helper only makes the suite able to *say*
    so.
    """
    return await asyncio.wait_for(scheduler.tick(), DRAIN_DEADLINE)


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

    ran = await _tick(scheduler)

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
    assert await _tick(_scheduler(job, clock=clock)) == 1
    assert job.runs == 1


async def test_a_job_that_has_never_run_is_due() -> None:
    """*"Never built"* and *"not due"* are the two states a naive
    `now - last_done > period` collapses -- with a `TypeError`, not a wrong
    answer, because `datetime - None` does not subtract.

    The neighbour rebuild on a fresh deployment is exactly this state, so it
    is the first one a registration will meet.
    """
    job = _Fake("never", last=None)
    assert await _tick(_scheduler(job)) == 1
    assert job.runs == 1


# -- the registry a composition root builds --------------------------------


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        **overrides,  # type: ignore[arg-type]
    )


def test_the_registry_a_composition_root_builds_holds_both_jobs_in_order() -> None:
    """**J4 shipped the loop with no registrations, J5 added the first and J6
    the second**, and a registry nothing asserts is indistinguishable from one
    somebody forgot to fill.

    ⚠️ **This case read `scheduler.jobs == ()` for one commit and then one
    name**, which is why each turn is a rewrite rather than a deletion: the
    point it makes is unchanged -- what a deployment will actually run is a
    line somebody has to write in `build_scheduler` -- and the value it asserts
    moves when a registration lands.

    **The order is asserted, not just the membership.** `Scheduler.tick` walks
    the registry in registration order and runs due jobs sequentially, so on a
    tick that finds both due the order decides whether a 0.072 ms prune waits
    behind a walk measured in hours or the other way round. A set comparison
    would be satisfied by either.

    Names rather than types. Each is a metric label
    (`usher.scheduler.job.duration` and its two siblings are all labelled
    `job`) and a span name, so a rename is an emptied panel; a case asserting
    `isinstance(..., SearchQueryRetention)` would let that through.
    """
    scheduler = build_scheduler(_settings(), sessions=_no_sessions())

    assert isinstance(scheduler, Scheduler)
    assert [job.name for job in scheduler.jobs] == [
        "search_queries.retention",
        "similar.rebuild",
    ]


def test_a_scheduler_with_no_way_to_reach_a_database_registers_nothing() -> None:
    """`sessions=None` is an explicit *"this process cannot reach a
    database"*, and an empty registry is still a legal state.

    The wrong implementation this kills: a `build_scheduler` that registered
    the retention job anyway and left it to fail on its first `last_done()` --
    which the loop would absorb, count, back off on and repeat forever, with
    the only symptom a log line every few minutes. Every `LaneSupervisor` in
    `tests/unit/test_api_lanes.py` is in this state.
    """
    scheduler = build_scheduler(_settings(), sessions=None)

    assert scheduler.jobs == ()


def test_the_retention_registration_carries_the_window_and_the_batch_an_operator_set() -> None:
    """The two settings reach the job, and the period comes from neither.

    The wrong implementations this kills: a registration that hard-codes 90
    days beside a setting an operator can change, which is the failure a
    setting exists to prevent; one that passes the *days* where a `timedelta`
    is wanted, which is a factor of 86,400 and reads as correct at a glance;
    one that wires the batch into the window or the window into the batch --
    two adjacent keyword arguments, so the names are all that stop a swap; and
    one that reads the *period* off the retention window, which is precisely
    the design ADR-0046 shipped with and `ScheduledJob.last_done` refuses.

    Read off the job's own declared configuration rather than its private
    attributes: `period`, `window` and `batch` are properties for this reason.
    Non-default values on both settings, because 90 and 10,000 are what a
    registration ignoring them would also produce.

    🔴 **The period is pinned to the literal and not to the constant**, the
    way the job's *name* already is one case above. `job.period ==
    RETENTION_PERIOD` compares the registration against the same symbol the
    composition root passes it, so it is a statement about the wiring and
    about nothing else -- measured 2026-09-07, moving `RETENTION_PERIOD` from
    `timedelta(days=1)` to `timedelta(days=30)` left this whole file green.
    **A day is a published number**: `.env.example`,
    `web/src/features/operator/Config.settings.ts`, PRD 08 and PRD 10 all
    state it in prose an operator reads, and a constant that moves under them
    is the same silent drift a renamed metric label is. Both assertions are
    kept -- the literal for the value, the symbol for the wiring.
    """
    scheduler = build_scheduler(
        _settings(search_query_retention_days=7, search_query_retention_batch=3),
        sessions=_no_sessions(),
    )

    job = next(one for one in scheduler.jobs if isinstance(one, SearchQueryRetention))
    assert job.window == timedelta(days=7)
    assert job.batch == 3
    assert timedelta(days=1) == RETENTION_PERIOD, (
        "the retention job offers itself once a day, and .env.example, Config.settings.ts, "
        "PRD 08 and PRD 10 all say so in prose no test reads"
    )
    assert job.period == RETENTION_PERIOD
    assert job.period != job.window, (
        "the period is the job's own and must not be read off the retention window"
    )


def test_the_rebuild_registration_carries_the_period_an_operator_set() -> None:
    """The one setting reaches the job, as a `timedelta` of **hours**.

    The wrong implementations this kills: a registration hard-coding 24 h
    beside a setting an operator can change; one passing the *hours* where a
    `timedelta` is wanted, which is a factor of 3,600 and reads as correct at a
    glance; and one wiring `timedelta(days=...)` from the retention setting
    next to it, which is the adjacent-keyword swap that made
    `SearchQueryRetention`'s own case necessary.

    **A non-default value, and one that is not a whole number of days**, so a
    registration ignoring the setting and a registration reading it through the
    wrong unit both fail rather than one of them.

    ⚠️ **A period here is a setting where retention's is a constant**, and this
    is the case that pins the asymmetry: what this number has to clear is the
    walk's own duration, which is a function of catalog size -- 3.58 h over
    132,442 seeds on this project's own artefact, 2026-08-19 -- and nothing in
    `src/` can know a deployment's.
    """
    scheduler = build_scheduler(
        _settings(similar_rebuild_period_hours=5.5), sessions=_no_sessions()
    )

    job = next(one for one in scheduler.jobs if isinstance(one, NeighborRebuildJob))
    assert job.period == timedelta(hours=5.5)
    assert job.name == "similar.rebuild"


# -- the retention registration (M10 J5) -----------------------------------


async def test_an_empty_table_is_not_due_rather_than_never_built() -> None:
    """🔴 **The defect ADR-0046's own reading had, arriving from the one state
    it handled correctly.**

    `ScheduledJob.last_done` says `None` means *"never built, therefore
    due"*, which is right for an artefact that has to be constructed and wrong
    for an **invariant**: an empty `search_queries` holds nothing past its
    cutoff, so the retention rule is satisfied vacuously. A `last_done()`
    answering `None` there would make an idle deployment run a no-op prune on
    every tick, forever -- the same "due on every tick" this job was
    redesigned to escape.

    Both halves are asserted, because *"the reading is not `None`"* is also
    what a job answering `datetime.min` would produce: the reading is `now`,
    and a tick over it runs nothing.
    """
    clock = _Clock()
    job = SearchQueryRetention(
        _scope_over(FakeSearchQueryRepository()),
        window=timedelta(days=90),
        batch=10,
        period=RETENTION_PERIOD,
        now=clock.read,
    )

    assert await job.last_done() == clock.now

    assert await _tick(_scheduler(job, clock=clock)) == 0


async def test_a_table_whose_oldest_row_is_inside_the_window_is_not_due() -> None:
    """The state this deployment is in today, and the state a healthy one is
    in almost always.

    The wrong implementation this kills is the one ADR-0046 tabulated:
    `last_done()` spelled as `min(at)` itself. A row 14 days old against a
    90-day window reads as *"last done 14 days ago"* under that spelling, i.e.
    due against any period under a fortnight -- and after a prune it would sit
    at the window's age and stay there, due forever. Here the same row reads
    as *"the invariant holds now"*.

    The control is the second arm: the same job, the same window, one row
    moved past the cutoff, must be due -- otherwise a `last_done()` that
    always answered `now` would pass the first half.
    """
    clock = _Clock()
    user_id = new_id()
    repository = FakeSearchQueryRepository()
    await repository.record(_row(at=clock.now - timedelta(days=14), user_id=user_id))
    job = SearchQueryRetention(
        _scope_over(repository),
        window=timedelta(days=90),
        batch=10,
        period=timedelta(days=1),
        now=clock.read,
    )

    assert await job.last_done() == clock.now
    assert await _tick(_scheduler(job, clock=clock)) == 0, "nothing is past the cutoff"

    await repository.record(_row(at=clock.now - timedelta(days=95), user_id=user_id))

    assert await job.last_done() == clock.now - timedelta(days=5)
    assert await _tick(_scheduler(job, clock=clock)) == 1, (
        "a row five days past a 90-day window is four days past a one-day period"
    )


async def test_the_period_is_how_much_expired_data_may_accumulate() -> None:
    """The arithmetic the reading buys, stated as a boundary.

    A job is due once the oldest surviving row is `window + period` old, so a
    row exactly `window + period` past is due and one a moment short of it is
    not. That is the whole difference between a period that decides something
    and ADR-0046's original reading, under which any period shorter than the
    window decided nothing at all.

    Two arms one microsecond apart, because a fixture a day either side of the
    boundary cannot tell `>=` from `>` -- and the not-due arm is what stops a
    job that is simply always due from passing.
    """
    clock = _Clock()
    window = timedelta(days=90)
    period = timedelta(days=1)
    user_id = new_id()

    short = FakeSearchQueryRepository()
    await short.record(
        _row(at=clock.now - window - period + timedelta(microseconds=1), user_id=user_id)
    )
    exact = FakeSearchQueryRepository()
    await exact.record(_row(at=clock.now - window - period, user_id=user_id))

    def job(repository: FakeSearchQueryRepository) -> SearchQueryRetention:
        return SearchQueryRetention(
            _scope_over(repository), window=window, batch=10, period=period, now=clock.read
        )

    assert await _tick(_scheduler(job(short), clock=clock)) == 0
    assert await _tick(_scheduler(job(exact), clock=clock)) == 1


async def test_a_run_moves_the_reading_its_own_period_is_compared_against() -> None:
    """🔴 **The contract `ScheduledJob.last_done` states, asserted for the
    first registration that owes it.**

    *"A reading this job's own runs move"* is the whole of why
    `min(search_queries.at)` was rejected. Here it is measured rather than
    argued: the job is due, it runs, and the same reading is `now` afterwards
    -- so the next tick does nothing.

    The premise guard is the first assertion: a job that was never due could
    not demonstrate anything by not running afterwards.

    The second half is the other direction, and it is what the rejected
    reading fails: a **new search** cannot move the reading. A row written
    at `now` is the newest one, so it changes neither `min(at)` nor the
    invariant, and the job stays not-due -- where under `min(at)`-as-a-
    completion-time a table that keeps being searched keeps ageing into
    permanent dueness.
    """
    clock = _Clock()
    user_id = new_id()
    repository = FakeSearchQueryRepository()
    await repository.record(_row(at=clock.now - timedelta(days=200), user_id=user_id))
    await repository.record(_row(at=clock.now - timedelta(days=10), user_id=user_id))
    job = SearchQueryRetention(
        _scope_over(repository),
        window=timedelta(days=90),
        batch=10,
        period=timedelta(days=1),
        now=clock.read,
    )
    scheduler = _scheduler(job, clock=clock)

    assert await _tick(scheduler) == 1, "the premise: this job was due"

    assert await job.last_done() == clock.now
    assert await _tick(scheduler) == 0, "its own run moved the reading past its own period"

    await repository.record(_row(at=clock.now, user_id=user_id))

    assert await _tick(scheduler) == 0, (
        "a fresh search must not make the retention job due -- that is the defect "
        "min(search_queries.at) as a last_done() has"
    )


async def test_the_prune_drains_in_chunks_and_opens_a_scope_for_each() -> None:
    """A commit per chunk, observed on this arm as a scope per chunk.

    The wrong implementations this kills: one `DELETE` for the whole
    population, which holds a transaction and a lock set over a table
    `GET /search` writes to on every request; a loop that reuses one scope, so
    every chunk commits at the end or not at all; and a loop that stops after
    the first chunk, which leaves the table over-length while reporting
    success.

    Seven expired rows against a batch of three: chunks of 3, 3, 1 -- and the
    short third is what terminates it, so **three** scopes and not four. The
    survivor arm is what stops a `run()` that simply emptied the table from
    passing.
    """
    clock = _Clock()
    user_id = new_id()
    repository = FakeSearchQueryRepository()
    for days in (200, 180, 160, 140, 120, 110, 100):
        await repository.record(_row(at=clock.now - timedelta(days=days), user_id=user_id))
    for days in (80, 1):
        await repository.record(_row(at=clock.now - timedelta(days=days), user_id=user_id))
    scope = _RecordingScope(repository)
    job = SearchQueryRetention(
        scope, window=timedelta(days=90), batch=3, period=RETENTION_PERIOD, now=clock.read
    )

    await _drain(job)

    assert scope.opened == 3, "3 + 3 + 1: the short chunk is the terminator"
    assert scope.closed_cleanly == 3, "every chunk's scope has to exit cleanly to commit"
    assert sorted((clock.now - record.at).days for record in repository.rows.values()) == [1, 80]


async def test_the_cutoff_is_taken_once_and_not_per_chunk() -> None:
    """A boundary recomputed inside its own loop moves under it.

    The wrong implementation this kills reads the clock per chunk, so a run
    that takes minutes deletes rows that were inside the window when it
    started. Driven with a clock that jumps a year between chunks and a batch
    of one: with the cutoff taken once, the row 10 days old survives;
    recomputed per chunk it is a year past the second chunk's cutoff and goes.

    This is also what makes the loop terminate against a live table -- a row
    written *during* the run is newer than a fixed cutoff by construction.
    """
    clock = _Clock()
    user_id = new_id()
    repository = FakeSearchQueryRepository()
    for days in (400, 380, 10):
        await repository.record(_row(at=clock.now - timedelta(days=days), user_id=user_id))

    def jumping_clock() -> datetime:
        reading = clock.now
        clock.now += timedelta(days=365)
        return reading

    job = SearchQueryRetention(
        _scope_over(repository),
        window=timedelta(days=90),
        batch=1,
        period=RETENTION_PERIOD,
        now=jumping_clock,
    )

    await _drain(job)

    survivors = [record.at for record in repository.rows.values()]
    assert len(survivors) == 1, "only the two rows past the *original* cutoff may go"


async def test_the_prune_says_how_many_rows_it_removed(lines: list[str]) -> None:
    """*"A filter is invisible without a counter"*, one table over.

    Without it, *"this deployment answered few searches"* and *"retention
    deleted them"* are the same observation. The wrong implementation this
    kills is a `run()` that returns quietly, which every other case here
    passes.
    """
    clock = _Clock()
    user_id = new_id()
    repository = FakeSearchQueryRepository()
    for days in (400, 380):
        await repository.record(_row(at=clock.now - timedelta(days=days), user_id=user_id))
    job = SearchQueryRetention(
        _scope_over(repository),
        window=timedelta(days=90),
        batch=10,
        period=RETENTION_PERIOD,
        now=clock.read,
    )

    await _drain(job)

    pruned = [line for line in lines if "pruned" in line]
    assert len(pruned) == 1, f"expected one line naming the count, got {pruned}"
    assert "pruned 2 " in pruned[0], pruned[0]


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

    assert await _tick(_scheduler(first, second, clock=clock)) == 2

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

    ran = await _tick(_scheduler(poison, healthy, clock=clock))

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

    assert await _tick(_scheduler(unreadable, healthy, clock=clock)) == 1

    assert unreadable.runs == 0, "a job ran on the strength of a read that raised"
    assert healthy.runs == 1
    assert [line for line in lines if "unreadable" in line]


class _NaiveLastDone(ScheduledJob):
    """A job whose `last_done()` answers a **timezone-naive** datetime.

    Not a hypothetical and not a hostile double: SQLAlchemy hands a naive
    value back for a `TIMESTAMP WITHOUT TIME ZONE` column,
    `ScheduledJob.last_done` states *"timezone-aware"* in prose, and nothing
    in the type system enforces it. This is the shape of the first
    registration that reads the wrong column type.
    """

    name = "naive"
    period = _HOUR

    async def last_done(self) -> datetime | None:
        return _NOW.replace(tzinfo=None) - timedelta(hours=2)

    async def run(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("a job whose reading could not be compared must not be run")


async def test_a_job_whose_last_done_is_naive_is_a_failure_and_not_a_dead_tick(
    lines: list[str],
) -> None:
    """🔴 **The due comparison was outside the guard for one commit, and this
    is what that cost.**

    `Scheduler._due_now` wrapped `await job.last_done()` and nothing else, so
    `now - last` on a naive answer raised `TypeError: can't subtract
    offset-naive and offset-aware datetimes` and escaped `tick()` entirely.
    Every job registered *after* the offender was skipped, on every tick,
    forever; `run()` logged it as *"the scheduler's tick failed"* with **no
    job name in it** -- the shape `LaneSupervisor._guard` exists to prevent --
    and under `usher schedule --once` it escaped the command altogether.

    Three assertions, and the sibling is the one that makes this about
    isolation rather than about a `TypeError`. It is registered **after** the
    offender deliberately: registration order is run order, so a sibling
    registered first would still run under the broken code and the case would
    pass against it.

    Found by J4's own review, relayed mid-task to J5 because J5's is the
    project's first real `last_done()`.
    """
    clock = _Clock()
    offender = _NaiveLastDone()
    sibling = _Fake("healthy", period=_HOUR, last=clock.now - timedelta(hours=2))
    scheduler = _scheduler(offender, sibling, clock=clock)

    ran = await _tick(scheduler)

    assert sibling.runs == 1, "a job registered after the offender was skipped by its failure"
    assert ran == 1, "the tick counted the sibling and not the job that could not be compared"
    assert [line for line in lines if "naive" in line and "last done" in line], (
        f"the failure has to name the job: {lines}"
    )


async def test_a_job_whose_last_done_is_naive_backs_off_rather_than_retrying_every_tick(
    lines: list[str],
) -> None:
    """The other half: an unusable reading is a failure of that job, so it is
    counted and spaced like one.

    The wrong implementation this kills is a `_due_now` that caught the
    `TypeError`, returned `False` and recorded nothing -- the loop would then
    ask a permanently broken job on every tick forever, which is the same hot
    loop `_back_off` exists to stop, arriving through the read instead of
    through the run.
    """
    clock = _Clock()
    offender = _NaiveLastDone()
    scheduler = _scheduler(offender, clock=clock)

    await _tick(scheduler)
    before = len([line for line in lines if "last done" in line])
    await _tick(scheduler)

    assert before == 1, "the first tick did not report the failure, so there is nothing to space"
    assert len([line for line in lines if "last done" in line]) == 1, (
        "a job whose reading could not be compared was asked again on the very next tick"
    )


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

    await _tick(scheduler)
    assert job.runs == 1, "the first tick did not run the job, so there is no failure to space"
    await _tick(scheduler)

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

    await _tick(scheduler)
    assert job.runs == 1

    clock.now += timedelta(minutes=59)
    await _tick(scheduler)
    assert job.runs == 1, "the backoff expired early"

    clock.now += timedelta(minutes=2)
    await _tick(scheduler)
    assert job.runs == 2, "the backoff never expired"

    # And the cap holds on the *second* failure, where an uncapped doubling
    # would ask for two periods.
    clock.now += timedelta(minutes=61)
    await _tick(scheduler)
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

    await _tick(scheduler)
    assert job.runs == 1
    job._fails = False
    clock.now += timedelta(minutes=2)
    await _tick(scheduler)
    assert job.runs == 2, "the premise: the backoff had expired and the job ran clean"

    # A clean run resets the counter, so the *next* failure is spaced by one
    # tick again rather than by two.
    job._fails = True
    await _tick(scheduler)
    assert job.runs == 3
    clock.now += timedelta(seconds=61)
    await _tick(scheduler)
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

    await _tick(scheduler)
    asked = job.asked
    await _tick(scheduler)

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
    await _tick(scheduler)

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

    await _tick(scheduler)

    assert "never" not in scheduler.read(), f"a never-run job reported {scheduler.read()}"


async def test_a_job_whose_last_done_raises_reports_no_due_point() -> None:
    """And the previous reading is dropped rather than left standing: a gauge
    still reporting a number for a job whose artefact cannot be read is the
    stale-but-wrong case the snapshot design exists to avoid."""
    clock = _Clock()
    job = _Fake("flaky", period=_HOUR, last=clock.now - timedelta(hours=3))
    scheduler = _scheduler(job, clock=clock)
    await _tick(scheduler)
    assert "flaky" in scheduler.read()

    job._last_done_fails = True
    await _tick(scheduler)

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
        await _tick(scheduler)
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
    **~71 ms** of database work for the one job that will exist -- one
    `last_done()` per registered job and nothing else, which is the whole of
    `ScheduledJob`'s contract -- so 0.12% of a minute at the floor and 7% of a
    second at one.

    ⚠️ **This docstring said ~144 ms, and that was ADR-0046's arithmetic over
    the wrong pair.** It added `count_stale()` to `computed_at()`; the first is
    the rebuild's own guard *inside* `run()` and not a read the loop performs,
    so it is a cost of the job rather than of the period. Corrected in the ADR
    and in `config.py`'s own table; recorded here because a test docstring that
    restates a figure is one of the places it goes stale. **The floor survives
    either figure** -- which is why this is an arithmetic correction and not a
    change to the bound the case asserts.
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
