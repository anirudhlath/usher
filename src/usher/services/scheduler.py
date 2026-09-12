"""The scheduler loop (ADR-0046). One `asyncio` task per deployment."""

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.trace import Link

from usher.ports.repository import SearchQueryRepository
from usher.ports.scheduler import JobOutcome, ScheduledJob

_tracer = trace.get_tracer("usher.scheduler")
_meter = metrics.get_meter("usher.scheduler")
# Seconds, like `usher.jobs.duration`.
_job_duration = _meter.create_histogram(
    "usher.scheduler.job.duration", unit="s", description="Wall time of one scheduled job's run"
)
_job_failures = _meter.create_counter(
    "usher.scheduler.job.failures",
    unit="1",
    description="Scheduled job runs that raised, by job",
)

# The most the retry backoff may double. `2 ** 20` ticks is 3.5 months at the
# 300 s default, which is far past every period this will ever hold -- the cap
# is here so a job failing for a year cannot turn an `int` exponent into an
# `OverflowError` inside the loop's own error handling.
_MAX_BACKOFF_DOUBLINGS = 20


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Backoff:
    """A job's failure streak and the instant it may next be offered.

    One record rather than two maps keyed alike, because the two halves are
    only ever written together and clearing one without the other would
    either space a healthy job out or retry a stuck one at the tick rate.
    """

    failures: int
    retry_after: datetime


class Scheduler:
    """The loop, and the registry it walks.

    Holds no session, no repository and no settings object: `tick_seconds` is
    a number the composition root read, and every job carries whatever it
    needs to reach a database. That is what lets a unit case drive the whole
    component over fakes with no database at all.
    """

    def __init__(
        self,
        *,
        tick_seconds: float,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._tick_seconds = tick_seconds
        # Injected only so a case can decide what "now" is without waiting for
        # it. Nothing in `src/` passes this -- and without it, "a job whose
        # period has not elapsed is not run" is a property no case could
        # observe in under an hour.
        self._now = now
        self._jobs: list[ScheduledJob] = []
        self._task: asyncio.Task[None] | None = None
        # Whether the "nothing is registered" line has been said. Per
        # scheduler rather than per tick -- see the module docstring.
        self._said_empty = False
        # PRD 10's `usher.scheduler.job.due`, as a synchronous snapshot. A new
        # mapping is assigned rather than the old one mutated, because the
        # reader runs on the metric reader's background thread and a rebind is
        # atomic where an in-place update is not.
        self._due: Mapping[str, float] = {}
        # The one piece of state this component holds, and it is about *this
        # process's* attempts rather than a durable last-run timestamp, which
        # ADR-0046 refuses. Without it a failed run is indistinguishable from
        # one never run, so a job that raises is due again on the very next
        # tick and retries at the tick rate forever.
        self._backoff: dict[str, _Backoff] = {}

    # -- the registry ----------------------------------------------------

    @property
    def jobs(self) -> tuple[ScheduledJob, ...]:
        """What is registered, in registration order -- which is the order a
        tick runs them in, and the only ordering there is."""
        return tuple(self._jobs)

    def register(self, job: ScheduledJob) -> None:
        """Add a job. Refuses a name already registered.

        The name is a metric label and a span name, so two jobs under one name
        make `usher.scheduler.job.duration` a histogram over two populations
        with nothing saying so -- the near-miss-name failure this project
        already records, arriving through a registry instead of a typo.
        """
        if any(existing.name == job.name for existing in self._jobs):
            raise ValueError(f"a scheduled job named {job.name!r} is already registered")
        self._jobs.append(job)

    # -- observation -----------------------------------------------------

    def read(self) -> Mapping[str, float]:
        """PRD 10's `usher.scheduler.job.due`: seconds since `last_done()` minus the
        period, per job. Negative means not due.
        """
        return self._due

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def task(self) -> asyncio.Task[None] | None:
        """The loop's task, kept after `stop()` rather than cleared.

        Reported so a case can assert the task **finished** rather than that
        the scheduler stopped reporting it: a `stop()` that dropped its
        reference and leaked the task answers `running() is False` just as
        readily. Same reason `LaneSupervisor.crashed_sources()` exists.
        """
        return self._task

    # -- the lifecycle ---------------------------------------------------

    async def start(self) -> None:
        """Create the loop task. **Awaits nothing, connects to nothing.**

        `create_app`'s lifespan builds an engine and opens no connection, and
        that is load-bearing: `/health` answers 200 with Postgres down while
        `/health/ready` reports 503. A `start()` that asked a job when it was
        last done would turn a database outage into a failure to boot, trading
        a documented, tested degradation for a worse one. The first
        `last_done()` therefore happens *inside* the loop task.

        `async` despite never suspending, because `stop()` is and because a
        lifespan calling one of a pair with `await` and the other without
        reads as a mistake -- `LaneSupervisor.start`'s own reasoning.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="usher.lane.scheduler")

    async def stop(self) -> None:
        """Cancel the loop and await it.

        An in-flight job is cancelled at its next `await`. `ScheduledJob.run`
        carries what that obliges an implementation to; the neighbour rebuild
        survives it because a cancelled page's transaction rolls back.

        `return_exceptions=True` because a task cancelled mid-await raises
        `CancelledError` here and one that had already crashed would re-raise
        whatever it crashed with -- neither may stop the rest of a shutdown.
        """
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        """Tick, then sleep -- in that order, so the first tick is this task's
        work rather than `start()`'s.

        The `except Exception` here is the loop's own boundary and is not the
        one that isolates a failing job: `tick` already catches per job, so
        anything arriving here is a failure no job owns. A loop that returned
        would leave the deployment with no scheduler and nothing saying so
        until the next restart, which is the same shape `_run_worker` refuses.
        """
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("the scheduler's tick failed: {error}", error=str(exc))
            await asyncio.sleep(self._tick_seconds)

    # -- one tick --------------------------------------------------------

    async def tick(self) -> int:
        """Walk the registry once and run whatever is due. Returns how many
        did the work, which is **not** how many were due.

        Two results are excluded: a job that raised, and one that answered
        `JobOutcome.DECLINED`. The number says *"how much work happened"*, and
        counting either as work is what makes a `--once` line from a cron read
        as healthy on a night nothing ran.
        """
        if not self._jobs:
            if not self._said_empty:
                # INFO rather than WARNING: an empty registry is the shipped
                # state of a deployment that turned the scheduler on before
                # anything registered a job, and it is legal.
                logger.info(
                    "the scheduler is running with no registered jobs, so a tick does "
                    "nothing; this is said once per process rather than once per tick"
                )
                self._said_empty = True
            return 0
        ran = 0
        for job in self._jobs:
            if not await self._due_now(job):
                continue
            if await self._run(job):
                self._backoff.pop(job.name, None)
                ran += 1
            else:
                self._back_off(job)
        return ran

    async def _due_now(self, job: ScheduledJob) -> bool:
        """Ask the artefact, and fold the answer into the gauge's snapshot."""
        now = self._now()
        held = self._backoff.get(job.name)
        if held is not None and now < held.retry_after:
            return False
        try:
            last = await job.last_done()
            # `overdue` rather than `age`, so the read, the subtraction and
            # the period all sit inside one guarded expression: `age >= period`
            # is `overdue >= 0`, and the gauge wants the difference anyway.
            overdue = None if last is None else (now - last) - job.period
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Not a run, so it is a failure of the job either way: a scheduler
            # that ran a multi-hour batch because a read did not answer is
            # worse than one that skipped a period.
            _job_failures.add(1, {"job": job.name})
            logger.exception(
                "the scheduled job {job} could not say when it was last done, so it was "
                "not run this tick: {error}",
                job=job.name,
                error=str(exc),
            )
            self._forget(job.name)
            self._back_off(job)
            return False
        if overdue is None:
            # Never built. Due, and with no age to report -- see `read()`.
            self._forget(job.name)
            return True
        self._observe(job.name, overdue.total_seconds())
        return overdue >= timedelta(0)

    async def _run(self, job: ScheduledJob) -> bool:
        """One job, inside its own root span. Returns whether it did the work.

        **A `JobOutcome.DECLINED` is neither timed nor counted as a failure**
        -- `JobOutcome` carries why -- and the caller spaces it out.

        **A root span with a `Link`, never a child**, and `context=Context()`
        -- an empty context -- is what makes "root" structural rather than a
        property of where the task happened to be created. `asyncio.create_task`
        copies the ambient context, so a lifespan or a test that started the
        scheduler inside a span would otherwise make every scheduled run a
        child of one request forever. PRD 10 specifies exactly this for a
        worker's `job.*`, and `rows.refresh` takes the same shape.

        **The `except Exception` is named and logged with the job name.**
        Without it the loop task dies and CPython reports the unretrieved
        exception at GC time, to stderr, with no job name in it -- the shape
        `LaneSupervisor._guard` exists for. `asyncio.CancelledError` is
        re-raised and never swallowed, so `stop()` works.
        """
        ambient = trace.get_current_span().get_span_context()
        links = [Link(ambient)] if ambient.is_valid else []
        started = time.perf_counter()
        outcome: JobOutcome | None = None
        try:
            with _tracer.start_as_current_span(
                f"scheduler.{job.name}", context=Context(), links=links
            ):
                outcome = await job.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _job_failures.add(1, {"job": job.name})
            logger.exception(
                "the scheduled job {job} failed; the scheduler carried on and will offer "
                "it again next tick: {error}",
                job=job.name,
                error=str(exc),
            )
            return False
        finally:
            # In a `finally` so a failed run is still timed: a batch that
            # raised after three hours is exactly the one an operator wants
            # the duration of. A refusal is the one thing not timed at all --
            # see `JobOutcome`.
            if outcome is not JobOutcome.DECLINED:
                _job_duration.record(time.perf_counter() - started, {"job": job.name})
        if outcome is JobOutcome.DECLINED:
            logger.info(
                "the scheduled job {job} declined to run, so it is spaced out rather than "
                "offered again on the next tick",
                job=job.name,
            )
            return False
        return True

    # -- the retry backoff -----------------------------------------------

    def _back_off(self, job: ScheduledJob) -> None:
        """Do not offer this job again for a doubling number of ticks, **capped at its own
        period**.
        """
        held = self._backoff.get(job.name)
        failures = (held.failures if held is not None else 0) + 1
        doublings = min(failures - 1, _MAX_BACKOFF_DOUBLINGS)
        delay = min(self._tick_seconds * (2**doublings), job.period.total_seconds())
        self._backoff[job.name] = _Backoff(failures, self._now() + timedelta(seconds=delay))

    # -- the gauge's snapshot --------------------------------------------

    def _observe(self, name: str, due_seconds: float) -> None:
        self._due = {**self._due, name: due_seconds}

    def _forget(self, name: str) -> None:
        if name in self._due:
            self._due = {key: value for key, value in self._due.items() if key != name}


# : How long one `search_queries` prune may leave the table over-length before : the
# scheduler offers the job again -- `SearchQueryRetention.period`, and : **one
# definition**: `composition.build_scheduler` passes this by name so a : registration
# reads with its period at the call site.
RETENTION_PERIOD = timedelta(days=1)

#: `SearchQueryRetention.name`. **Stable, because it is a metric label**
#: (`usher.scheduler.job.duration`, `.failures` and `.due` are all labelled
#: `job`) and a span name (`scheduler.search_queries.retention`), and a
#: renamed label is an emptied panel and a histogram split across two
#: populations.
RETENTION_JOB_NAME = "search_queries.retention"

# : One `SearchQueryRepository`, in a scope that **commits on a clean exit**.
SearchQueryScope = Callable[[], AbstractAsyncContextManager[SearchQueryRepository]]


class SearchQueryRetention(ScheduledJob):
    """PRD 10's 90-day `search_queries` prune, as a scheduled job (M10's J5)."""

    name = RETENTION_JOB_NAME

    def __init__(
        self,
        scope: SearchQueryScope,
        *,
        window: timedelta,
        batch: int,
        period: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        # The drain's terminator is a chunk shorter than the limit, so a limit
        # below 1 never produces one. Refused here rather than left to
        # `Settings.search_query_retention_batch`'s `ge=1`, which is two layers
        # away and is not reached by a job built any other way.
        if batch < 1:
            raise ValueError(f"a retention batch of {batch} deletes nothing and never drains")
        self._scope = scope
        self._window = window
        self._batch = batch
        self._period = period
        # Injected for `Scheduler`'s reason and one more: the whole point of
        # this job is a boundary, and a case that asked "is a row 91 days old"
        # against a real clock is a case that changes its answer at midnight.
        self._now = now

    @property
    def period(self) -> timedelta:
        return self._period

    async def last_done(self) -> datetime | None:
        """`min(min(at) + window, now)` -- see the class docstring.

        **The cap at `now` is what makes this a *last* anything.** Without it
        an untouched table answers a time in the future, which is not a
        completion; the scheduler would still read it as not-due (a negative
        age), so the cap changes no decision -- what it changes is
        `Scheduler.read()`'s series, which would otherwise carry an
        unboundedly negative *"due in"* for a table nobody has searched
        against in a year.

        One scope, one aggregate, and the scope is closed before the clock is
        read so the reading is never older than the query. The two clocks in
        play -- this one and the scheduler's, taken a moment earlier -- can
        differ by microseconds, and the skew can only ever make the job
        *less* due, because a `last_done()` slightly in the scheduler's future
        subtracts to a negative age.
        """
        async with self._scope() as queries:
            oldest = await queries.oldest()
        now = self._now()
        if oldest is None:
            # An empty table satisfies the retention rule vacuously, so the
            # invariant holds *now*. `None` here would mean "due", forever.
            return now
        return min(oldest + self._window, now)

    async def run(self) -> JobOutcome:
        """Delete everything past the cutoff, `batch` rows and one transaction at a time."""
        cutoff = self._now() - self._window
        removed = 0
        while True:
            async with self._scope() as queries:
                deleted = await queries.prune(before=cutoff, limit=self._batch)
            removed += deleted
            # A short chunk is an exhausted predicate, and `__init__`'s floor
            # on `batch` is what keeps one reachable.
            if deleted < self._batch:
                break
        logger.info(
            "pruned {removed} search_queries rows answered before {cutoff}",
            removed=removed,
            cutoff=cutoff.isoformat(),
        )
        # Never declined: a prune of an already-clean table is a no-op the
        # scheduler should still count, because the invariant it maintains now
        # holds at `now` and `last_done()` says so.
        return JobOutcome.DONE


__all__ = [
    "RETENTION_JOB_NAME",
    "RETENTION_PERIOD",
    "Scheduler",
    "SearchQueryRetention",
    "SearchQueryScope",
]
