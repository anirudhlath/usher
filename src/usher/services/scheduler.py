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
`last_done()` per registered job and nothing else** -- 71.1 ms for the one
registration to come -- so 60 s is 0.12% duty and 1 s would be 7%.

**And it holds one piece of state, deliberately: a per-job retry backoff.**
ADR-0046's *"the scheduler stores nothing"* is about a durable last-run
timestamp, and this is not one -- it is a fact about this process's attempts,
which no artefact carries. Without it the loop has a hole its own acceptance
criterion cannot see: with nothing recording that a run failed, a failing job
is due again on the very next tick and retries at the tick rate forever. See
`_back_off`, which also states the half it does **not** fix -- a retry that
restarts from page one does not converge however far apart the attempts are,
and resumption belongs to the registration.

**The registry ships empty**, and that is a legal, observable state rather than
an unfinished one. J5 (`search_queries` retention) and J6 (the neighbour
rebuild) are the two registrations, each a task of its own, and
`composition.build_scheduler` is where they go. A tick over zero jobs logs
**once** -- not once per tick -- and sleeps, because a line every five minutes
forever is the shape an operator mutes and then never sees the real one.

**This module imports `usher.ports` and stdlib and nothing else from this
project**, which is what keeps it inside contracts 1-3 without a contract of
its own: `usher.services` reaching `usher.db` or `usher.adapters` breaks two
contracts that report indirect chains by default. The two instruments below are
declared against `opentelemetry` directly, the way `services/jobs.py` declares
`usher.jobs.duration`; the observable gauge cannot be, and lives in
`usher.telemetry` for the reason `read()` states.
"""

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.trace import Link

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
        # 🔴 **The retry backoff, and it is the one piece of state this
        # component holds.** ADR-0046's *"the scheduler stores nothing"* is
        # about a **durable** last-run timestamp -- a second copy of a fact the
        # artefact already carries, which drifts the first time an operator
        # runs the command by hand. These two are neither: they are facts about
        # *this process's* attempts, no artefact carries them, and losing them
        # on a restart is correct rather than a defect.
        #
        # Without them the loop has a hole its own acceptance criterion cannot
        # see. *"A failing job does not stop the loop"* is satisfied by a loop
        # that also never progresses: with no stored state a **failed** run is
        # indistinguishable from one never run, so a job that raises leaves
        # `last_done()` exactly where it was, is due again on the very next
        # tick, and retries forever at the tick rate with nothing between
        # attempts. At the 300 s default that is 288 attempts a day against a
        # database that is, by hypothesis, already unhappy.
        self._consecutive_failures: dict[str, int] = {}
        self._retry_after: dict[str, datetime] = {}

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
                self._consecutive_failures.pop(job.name, None)
                self._retry_after.pop(job.name, None)
                ran += 1
            else:
                self._back_off(job)
        return ran

    async def _due_now(self, job: ScheduledJob) -> bool:
        """Ask the artefact, and fold the answer into the gauge's snapshot.

        **One `last_done()` per job per tick, and nothing else**, which is the
        whole of what a tick costs before any job runs -- 71.1 ms measured for
        the one registration to come (`config.py` carries the table). Both
        halves of the answer come from that single read, so the gauge cannot
        disagree with the decision.

        ⚠️ **A job inside its retry backoff is skipped before the read**, so
        its gauge entry is frozen at the last reading rather than refreshed.
        That is the price of not paying for a query about a job this process
        has already decided not to offer, and the failure counter and the
        logged exception are what say the job is in that state.
        """
        now = self._now()
        retry_after = self._retry_after.get(job.name)
        if retry_after is not None and now < retry_after:
            return False
        try:
            last = await job.last_done()
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
        if last is None:
            # Never built. Due, and with no age to report -- see `read()`.
            self._forget(job.name)
            return True
        age = now - last
        self._observe(job.name, age.total_seconds() - job.period.total_seconds())
        return age >= job.period

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
        failures = self._consecutive_failures.get(job.name, 0) + 1
        self._consecutive_failures[job.name] = failures
        doublings = min(failures - 1, _MAX_BACKOFF_DOUBLINGS)
        delay = min(self._tick_seconds * (2**doublings), job.period.total_seconds())
        self._retry_after[job.name] = self._now() + timedelta(seconds=delay)

    # -- the gauge's snapshot --------------------------------------------

    def _observe(self, name: str, due_seconds: float) -> None:
        self._due = {**self._due, name: due_seconds}

    def _forget(self, name: str) -> None:
        if name in self._due:
            self._due = {key: value for key, value in self._due.items() if key != name}


__all__ = ["Scheduler"]
