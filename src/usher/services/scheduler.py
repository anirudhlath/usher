"""The scheduler loop (ADR-0046). One `asyncio` task per deployment.

Per tick it walks the registry, asks each job `last_done()`, and runs the ones
whose period has elapsed **sequentially, one at a time**. Then it sleeps a
fixed `USHER_SCHEDULER_TICK_SECONDS` and does it again.

**Three things it is deliberately not**, each because the alternative has a
measured cost somewhere in this repository:

- **Not a `TaskGroup` and not `gather`.** ADR-0037's argument applies unchanged
  and one abstraction lower: a task group cancels its siblings on the first
  escape, which turns one poisoned job into a rebuild abandoned mid-page.
  `JobWorker` reaches for `asyncio.wait` for that reason; this component needs
  no primitive at all, because it runs one job at a time on purpose.
- **Not one task per job.** That is what makes *"two hours-long jobs contending
  for the same connection pool"* reachable with nothing bounding it. Sequential
  within a tick is the bound, and it costs a tick's latency on the second job,
  which against a period measured in hours is not a cost.
- **Not a sleep-until-next-due calculation.** A due time computed from a
  `last_done()` that another process may have moved is a cache of a fact the
  artefact already answers -- and this component's whole design (ADR-0046) is
  that it caches nothing. A fixed tick re-asks instead.

**The tick has a measured floor and it is `Field(ge=60.0)` on the setting.**
`config.py` carries the table; the short form is that a tick issues **one
`last_done()` per registered job and decides on nothing else** -- 71.1 ms for
the neighbour rebuild's reading and 0.041 ms for `SearchQueryRetention`'s
(re-measured 2026-09-07 on `usher_j2`) -- so 60 s is 0.12% duty and 1 s would
be 7%. ⚠️ *"And nothing else"* was this paragraph's claim until 2026-09-07 and
overstates it by one round trip per job: `SearchQueryRetention.last_done` reads
through a `SearchQueryScope`, which commits on a clean exit, so the read is a
pool checkout, a `SELECT` and a `COMMIT`. See `_due_now`.

**And it holds one piece of state, deliberately: a per-job retry backoff.**
ADR-0046's *"the scheduler stores nothing"* is about a durable last-run
timestamp, and this is not one -- it is a fact about this process's attempts,
which no artefact carries. Without it the loop has a hole its own acceptance
criterion cannot see: with nothing recording that a run failed, a failing job
is due again on the very next tick and retries at the tick rate forever. See
`_back_off`, which also states the half it does **not** fix -- a retry that
restarts from page one does not converge however far apart the attempts are,
and resumption belongs to the registration.

**The registry shipped empty for one commit and holds two jobs now.**
`SearchQueryRetention` below is M10's J5 and
`usher.services.similar.NeighborRebuildJob` is J6; `composition.
build_scheduler` registers both, retention first, so a tick that finds both
due spends a chunk on the prune before starting a walk measured in hours. An
empty registry remains a **legal** state rather than an unfinished one -- a
composition root with no way to reach a database builds one -- and a tick over
zero jobs logs **once**, not once per tick, because a line every five minutes
forever is the shape an operator mutes and then never sees the real one.

**One registration lives here and one does not, and the split is the rule
rather than the exception.** `SearchQueryRetention` is here because it has no
service of its own: it is thirty lines over one repository, and the argument it
exists to carry -- what a `last_done()` may be read off -- is the argument this
module's loop is built on. J6's belongs beside `SimilarityService`, because the
batch, the blend fingerprint and the resume cursor its `run()` needs are all
that module's, and importing them here would put `usher.services.similar` into
a module whose whole claim is that it reaches nothing but `usher.ports`. **The
rule is that a registration lives with the artefact it maintains**, and
retention is the case where that is this file.

**This module imports `usher.ports` and stdlib and nothing else from this
project, J6's registration included**, which is what keeps it inside contracts
1-3 without a contract of its own: `usher.services` reaching `usher.db` or
`usher.adapters` breaks two contracts that report indirect chains by default. That is also why
`SearchQueryScope` is a callable returning a context manager rather than a
session factory -- `composition.UnitOfWork`'s shape, for
`composition.UnitOfWork`'s reason. The two instruments below are
declared against `opentelemetry` directly, the way `services/jobs.py` declares
`usher.jobs.duration`; the observable gauge cannot be, and lives in
`usher.telemetry` for the reason `read()` states.
"""

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
from usher.ports.scheduler import ScheduledJob

_tracer = trace.get_tracer("usher.scheduler")
_meter = metrics.get_meter("usher.scheduler")
# Seconds, like `usher.jobs.duration`. ⚠️ PRD 10 records that every
# seconds-unit histogram in this project is currently unreadable below five
# seconds -- `configure_metrics` installs no `View`, so the SDK's default
# bucket boundaries `(0.0, 5.0, 10.0, 25.0, ...)` apply. This is the one
# histogram in the catalogue that is *not* hurt by it: both registrations that
# will exist are measured in hours.
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
        """PRD 10's `usher.scheduler.job.due`: seconds since `last_done()`
        minus the period, per job. Negative means not due.

        **Synchronous, and a snapshot rather than a query.** OTel invokes an
        observable callback from the metric reader's *background thread*, and
        every `last_done()` on the shipped registrations is a coroutine on
        asyncpg -- so a callback that queried would have to bounce a coroutine
        onto the event loop and block the exporter thread on it, which
        deadlocks whenever the loop is itself blocked
        (`.claude/rules/api-telemetry-and-lanes.md`). Exactly the shape
        `register_queue_gauges` and `register_search_gauges` already take.

        Stale, never wrong: the value is the reading the last tick took, so
        during a run the same tick started it reports the moment the job
        became due rather than a number that keeps growing. A job inside its
        retry backoff is skipped before the read, so its entry is frozen for
        as long as the spacing lasts -- the failure counter is what says the
        job is in that state, not this series.

        **A job with no reading has no entry**, and there are two ways to have
        none -- never run, and a `last_done()` that raised. A fabricated `0.0`
        would read as *"exactly due"* in both, which is the one value that
        makes the series wrong rather than merely absent (`_observations`' own
        rule, in `usher.telemetry`).
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
        ran to completion.

        A job that raised is **not** counted: the number answers *"how much
        work happened"*, and counting a failure as work is what makes a
        `--once` line from a cron read as healthy on a night nothing ran.
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
        """Ask the artefact, and fold the answer into the gauge's snapshot.

        **One `last_done()` per job per tick and no second question**, which is
        the whole of what a tick costs before any job runs -- 71.1 ms measured
        for the neighbour rebuild's reading and 0.041 ms for retention's
        (`config.py` carries the table). Both halves of the answer come from
        that single read, so the gauge cannot disagree with the decision.

        ⚠️ **One *call* is not one round trip, and this docstring said
        "nothing else" until 2026-09-07.** What a registration does inside its
        `last_done()` is its own business, and the one that ships spends two:
        `SearchQueryRetention` reads through a `SearchQueryScope`, and
        `composition.search_query_scope` **commits on a clean exit**, so a
        read-only `SELECT min(at)` is a pool checkout, the `SELECT`, and a
        `COMMIT` over a transaction that wrote nothing. It is cheap and it is
        not free, and the scheduler cannot see it -- the commit is what makes
        the *prune's* chunking durable (`run()`), so it is a property of the
        scope rather than something a read could opt out of.

        🔴 **The subtraction is inside the guard, and it was outside it for
        one commit.** `await job.last_done()` was the only thing wrapped, so a
        job answering a **timezone-naive** datetime raised `TypeError: can't
        subtract offset-naive and offset-aware datetimes` from `now - last`,
        which escaped `tick()` entirely: every job registered after the
        offender was skipped on **every** tick, forever, logged by `run()` as
        *"the scheduler's tick failed"* with **no job name in it** -- verbatim
        the shape `LaneSupervisor._guard` exists to prevent, and under
        `usher schedule --once` it escapes the command altogether. A naive
        datetime is not a hypothetical: SQLAlchemy hands one back for a
        `TIMESTAMP WITHOUT TIME ZONE` column, `ScheduledJob.last_done` states
        *"timezone-aware"* in prose and nothing enforces it, and J5's is the
        first real `last_done()` in the project. `job.period` is inside the
        guard for the same reason -- it is a property an implementation
        computes, and a raising one is the same failure one attribute over.
        `test_a_job_whose_last_done_is_naive_is_a_failure_and_not_a_dead_tick`
        is what pins it.

        ⚠️ **A job inside its retry backoff is skipped before the read**, so
        its gauge entry is frozen at the last reading rather than refreshed.
        That is the price of not paying for a query about a job this process
        has already decided not to offer, and the failure counter and the
        logged exception are what say the job is in that state.
        """
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
        """One job, inside its own root span. Returns whether it completed.

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
        try:
            with _tracer.start_as_current_span(
                f"scheduler.{job.name}", context=Context(), links=links
            ):
                await job.run()
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
            # the duration of.
            _job_duration.record(time.perf_counter() - started, {"job": job.name})
        return True

    # -- the retry backoff -----------------------------------------------

    def _back_off(self, job: ScheduledJob) -> None:
        """Do not offer this job again for a doubling number of ticks,
        **capped at its own period**.

        The cap is what makes this safe rather than a second schedule: a job
        that keeps failing settles to being retried no more often than the
        period it declared, which is exactly the schedule it would have had if
        every attempt had succeeded. So the worst this can do is *stop* a hot
        loop; it can never delay a job past its own period.

        The first retry is one tick later -- i.e. no added delay at all -- so a
        one-off blip costs nothing, and only a job that is genuinely stuck
        backs off. `_MAX_BACKOFF_DOUBLINGS` bounds the exponent.

        ⚠️ **What this does not fix, stated because the acceptance criterion
        it serves cannot see the difference.** A backoff bounds the *rate* of
        retries; it does not make a retry converge. `SimilarityService.rebuild`
        restarts from the first page every run, so a walk that dies at 60%
        leaves the artefact exactly as it was and the next attempt redoes the
        60% before reaching new work -- and no amount of spacing turns that
        into progress. **Resumption is the registration's problem, not the
        loop's**, which is why `ScheduledJob.run` obliges an implementation to
        be re-runnable and why the neighbour rebuild's resume is J6's task
        rather than something this component can supply.

        And it is per process. A `usher schedule --once` from a crontab starts
        with an empty backoff every time, which is correct -- a fresh process
        has no evidence about anything -- and means an operator driving the
        scheduler that way gets the retry rate of their own cron.
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


#: How long one `search_queries` prune may leave the table over-length before
#: the scheduler offers the job again -- `SearchQueryRetention.period`, and
#: **one definition**: `composition.build_scheduler` passes this by name so a
#: registration reads with its period at the call site.
#:
#: **A day, and the arithmetic is the argument.** The period is not the
#: retention window: with `last_done()` spelled as below, a job is due once
#: the oldest surviving row is `window + period` old, so this number is *how
#: much expired data may accumulate*, not how long a row is kept. A day of it
#: is a small fraction of one chunk -- measured 2026-09-07 on `usher_j2`, a
#: clone of the live catalog, organic `search_queries` arrivals run at **5.6
#: rows a day** against a 10,000-row chunk -- and it means a prune lands within
#: a day of a row expiring rather than within a tick of it. Shorter buys
#: nothing an operator can see; longer would let the table run measurably over
#: its stated window.
#:
#: ⚠️ **This docstring read *"14,978 rows in 14 d 06 h, so a day is ~1,050
#: rows"* until 2026-09-07, and that is a burst divided by a span it did not
#: arrive over.** Re-measured on the same clone: 14,898 of the 14,978 rows
#: (99.5%) carry `surface = 'suggest'` and landed on a single day, 2026-08-27,
#: from J2's own backfill; the organic remainder is 80 rows; and the span is
#: 14 d 04 h 11 m, not 14 d 06 h. The conclusion survives and gets stronger --
#: at single digits a day the steady-state prune is emphatically one chunk --
#: but the burst is what a *first* run after the suggest writer is switched on
#: looks like, not a daily rate. `config.py` carries the three databases the
#: figure was re-derived over.
#:
#: **A day is also a published number.** `.env.example`,
#: `web/src/features/operator/Config.settings.ts`, PRD 08 and PRD 10 all state
#: it in prose an operator reads, so it is pinned to its literal in
#: `tests/unit/test_services_scheduler.py::
#: test_the_retention_registration_carries_the_window_and_the_batch_an_operator_set`
#: rather than only compared against itself.
#:
#: A **property of the job and not a setting**, which is the shape
#: `ScheduledJob.period` and `cli._schedule` both already state: an operator
#: tunes the *window* (`USHER_SEARCH_QUERY_RETENTION_DAYS`), which is the
#: number PRD 10 prices and the one a household would ask about.
RETENTION_PERIOD = timedelta(days=1)

#: `SearchQueryRetention.name`. **Stable, because it is a metric label**
#: (`usher.scheduler.job.duration`, `.failures` and `.due` are all labelled
#: `job`) and a span name (`scheduler.search_queries.retention`), and a
#: renamed label is an emptied panel and a histogram split across two
#: populations.
RETENTION_JOB_NAME = "search_queries.retention"

#: One `SearchQueryRepository`, in a scope that **commits on a clean exit**.
#:
#: A callable rather than a session factory, for `composition.UnitOfWork`'s
#: own reason one layer up: it is what lets `usher.services` and
#: `usher.api.lanes` reach a database without either of them importing
#: SQLAlchemy, and what lets a unit case drive the whole component over a fake
#: with no database at all. The commit belongs to the scope rather than to the
#: repository because every repository in this project flushes and never
#: commits -- and `SearchQueryRetention.run` opens **one scope per chunk**,
#: which is how "a commit per chunk" is expressed without this module knowing
#: what a transaction is.
SearchQueryScope = Callable[[], AbstractAsyncContextManager[SearchQueryRepository]]


class SearchQueryRetention(ScheduledJob):
    """PRD 10's 90-day `search_queries` prune, as a scheduled job (M10's J5).

    The first registration ADR-0046 gets, and **the one that had to answer
    that record's open question rather than inherit it.**

    🔴 **`min(search_queries.at)` is not a `last_done()`, and this is what
    replaced it.** ADR-0046's decision-2 table gave retention's reading as
    `min(at)` and called it exact. It is the age of the **oldest surviving
    row**, written by the search path rather than by this job, so after a
    prune it sits at the window's age and stays there while rows keep ageing
    in from the other end -- the job reads as due on every tick, forever, for
    any period shorter than the window, and `period` decides nothing.
    `ScheduledJob.last_done` now makes *"a reading this job's own runs move"*
    the contract, and this class owes it.

    **The artefact this job maintains is not a row; it is the table's lower
    bound**, and that is what a completion time can be read off. A successful
    run at instant *T* establishes *"no row is older than T - window"*, so the
    invariant held at *T*, and it goes on holding until the oldest surviving
    row itself falls out of the window. Hence:

        last_done() = min(min(at) + window, now)

    -- *"the most recent instant at which this table was known to hold nothing
    past its cutoff"*. **This job's own runs move it and nothing else does**:
    a prune pushes `min(at)` forward to at least `now - window`, which pushes
    the reading to `now`; a search writing a *new* row cannot move `min(at)`
    at all, because a new row is the newest one. That is the whole difference
    from the reading it replaces.

    The period then reads honestly against it: a job is due once the oldest
    surviving row is `window + period` old, i.e. once a period's worth of
    expired rows has accumulated. See `RETENTION_PERIOD`.

    ⚠️ **`last_done()` never answers `None`, and that is a decision rather
    than an accident.** `None` means *"never built, therefore due"*, which is
    right for an artefact that has to be constructed and wrong for an
    invariant: an **empty** `search_queries` satisfies the retention rule
    vacuously, so answering `None` there would make an idle deployment run a
    no-op prune on every tick forever -- the identical defect, arriving from
    the one state the original reading handled correctly. An empty table
    answers `now`.

    **A failed run converges, unlike the other registration.** ADR-0046
    records that a run which dies part-way is indistinguishable from one that
    never ran, and that `Scheduler._back_off` bounds the retry *rate* rather
    than the progress -- true, and the reason is `SimilarityService.rebuild`
    restarting from page one. A prune does not: every committed chunk removes
    rows permanently, so an interrupted run has made progress the next one
    keeps, and the chunks go **oldest first** so the progress is the rows
    furthest past the window. `run()` is therefore safe to cancel at any
    `await` and safe to run twice, which is what `ScheduledJob.run` obliges.
    """

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

    async def run(self) -> None:
        """Delete everything past the cutoff, `batch` rows and one
        transaction at a time.

        **The cutoff is computed once, before the first chunk.** A boundary
        recomputed per chunk moves under its own loop, which is a third bug
        beside the two `now()`/`clock_timestamp()` traps
        `.claude/rules/db-and-sql.md` already carries -- and a fixed cutoff is
        also what makes the loop terminate against a table `GET /search` is
        writing to throughout: a row arriving mid-run is newer than the
        cutoff by construction and is not a row this run is looking for.

        **A scope per chunk, so a commit per chunk.** A single `DELETE` over a
        year of keystrokes would hold one transaction and one lock set for its
        whole duration on a table every answered search writes to. It also
        makes an interrupted run keep whatever it has already committed.

        **The terminator is `deleted < batch`, and it needs no keyset.**
        `prune` answers the rows it actually removed, so a short chunk is an
        exhausted predicate -- and a deleted row cannot re-satisfy `at <
        cutoff`, where `SimilarityService.rebuild`'s *"re-read what looks
        stale, rebuild, repeat"* does not terminate against a row the
        predicate cannot clear. That is the difference that lets this loop be
        three lines rather than a cursor.

        🔴 **And *"a deleted row cannot re-satisfy the predicate"* is a claim
        about a committed delete, so the terminator has a precondition this
        module cannot enforce: `SearchQueryScope` must commit each chunk.**
        Each iteration opens a new scope and therefore a new session; an
        *uncommitted* delete is invisible to the next one, which re-selects the
        same rows, deletes them again, and answers the same full-length chunk
        forever. It is the shipped wiring --
        `composition.search_query_scope` commits on a clean exit -- but it is a
        property of the callable a composition root passes, not of this loop.
        Measured 2026-09-07: deleting that one `await session.commit()` turns
        this drain into a non-terminating loop, caught by
        `tests/integration/test_search_query_retention.py`'s `DRAIN_DEADLINE`
        as a `TimeoutError` rather than by any assertion here. A scope that
        did not commit would also be a prune that deleted nothing durably, so
        the two failures are one defect and the case that owns it is
        `test_the_prune_commits_each_chunk_where_a_composition_root_wired_it`.

        The other half of the terminator is that `batch` is at least 1, which
        `__init__` refuses to accept otherwise: at `batch = 0` a chunk deletes
        nothing and `0 < 0` is false.

        The count is logged rather than counted on an instrument: this runs
        once a day, and *"a filter is invisible without a counter"* is
        satisfied by something that can say how often it fired rather than by
        a particular mechanism. A fourth metric row would move PRD 10's
        maintained instrument count at four sites for a number an operator
        reads once a day.
        """
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


__all__ = [
    "RETENTION_JOB_NAME",
    "RETENTION_PERIOD",
    "Scheduler",
    "SearchQueryRetention",
    "SearchQueryScope",
]
