"""The scheduled-work port (ADR-0046, and PRD 08's "nothing runs
`usher similar --rebuild` for you").

A **name**, a **period**, a `last_done()` and a `run()`. There is no crontab
expression, no calendar, no timezone, no per-job concurrency and no dependency
graph, because both customers this exists for take hours and neither needs any
of it. `usher.services.scheduler.Scheduler` is the loop; this file is only the
shape a job has to have to be registered with it.

**The split is `ports/jobs.py`/`services/jobs.py`'s, one abstraction lower
because there is no queue.** `JobKind` is what this is deliberately *not*: a
queue whose unit of work is one row and whose trigger is an event, which
`SimilarityService.rebuild`'s own docstring already refuses on exactly these
grounds -- *"a job kind whose trigger is a timer is a cron entry with a queue
and a park path bolted on"*.

🔴 **`last_done()` comes from the artefact the job maintains, never from the
scheduler**, and that is the contested half of ADR-0046 rather than an
implementation detail. A stored last-run timestamp would be a second copy of a
fact the artefact already carries, and the two drift the first time an operator
runs the command by hand -- which is exactly how the neighbour rebuild is run
today. A job that produces no artefact therefore cannot be registered without a
design change; the third registration is the test of that, and the shape to
watch for is a job whose work is a side effect (a cache warm, a health probe, a
notification), because those have nothing to read a completion time off.

**`abc.ABC`, not `typing.Protocol` (ADR-0001), and this file does not reopen
it.** `name` and `period` are abstract *properties* rather than bare
annotations for the same reason: an annotation is satisfied by an
implementation that never assigns it, so the first thing that would notice is a
metric label reading `AttributeError` at the first tick. A class attribute
satisfies either spelling, so an implementation pays nothing for the
enforcement -- `name = "neighbors.rebuild"` on the subclass is enough.

This module imports `datetime` and `abc` and nothing from this project, which
is what keeps it inside contracts 1-4 without a contract of its own: `usher.ports`
may not import `usher.db`, `usher.adapters` or `usher.config`, and there is
nothing here that would want to.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import StrEnum


class JobOutcome(StrEnum):
    """What one `ScheduledJob.run()` amounted to, and what the loop does with
    each. **The one statement of this; every other site points here.**

    `DECLINED` is a run that never started, because the deployment is in a
    state no retry fixes. It is neither work nor failure, so it is counted in
    neither place: a refusal's milliseconds in `usher.scheduler.job.duration`
    would read as a fast run of a job measured in hours, and
    `usher.scheduler.job.failures` is for a job that tried and broke. What it
    does get is a failure's *spacing*, which is what stops the refusal being
    logged on every tick for as long as an operator leaves it.
    """

    DONE = "done"
    DECLINED = "declined"


class ScheduledJob(ABC):
    """One named batch the scheduler may run on a period."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable, and used as a metric label and a span name.

        `usher.scheduler.job.duration`, `.failures` and `.due` are all
        labelled `job` with this string, and the run's span is
        `scheduler.<name>` -- so renaming one empties a panel and splits a
        histogram across two series, which is the same silent failure a
        near-miss metric name produces. Two jobs may not share it, and
        `Scheduler.register` refuses rather than letting one label cover two
        populations.
        """

    @property
    @abstractmethod
    def period(self) -> timedelta:
        """The **minimum interval since the last completion**, never a
        wall-clock schedule.

        *"Every night at 3am"* is not expressible here and is deliberately
        not offered: an operator who wants that runs `usher schedule --once`
        from their own cron, which is the pre-M10 arrangement kept as a
        supported path rather than replaced (ADR-0046).

        A job is due when `now - last_done() >= period`, so a period of one
        hour means *"at least an hour has passed"* and not *"more than an
        hour"*. The scheduler compares this against a `last_done()` another
        process may have moved, which is why it is a comparison per tick
        rather than a due-time this component caches.
        """

    @abstractmethod
    async def last_done(self) -> datetime | None:
        """When this job's **artefact** was last complete, or `None` if it
        has never been built. Timezone-aware.

        Read from the artefact, never from the scheduler -- see the module
        docstring.

        **`None` means due, and that is settled here rather than left to be
        inferred.** ADR-0046 only implies it, through its fresh-deployment
        argument; the scheduler acts on it and
        `tests/unit/test_services_scheduler.py::
        test_a_job_that_has_never_run_is_due` is what pins it. *"Never built"*
        and *"not due"* are the two states a naive `now - last_done > period`
        collapses, with a `TypeError` rather than a wrong answer.

        🔴 **It must be a *completion* time, and "some timestamp on the
        artefact" is not the same thing.** The reading has to be one this job's
        own runs move; a reading that ages on its own, driven by anything other
        than this job completing, makes the period inoperative -- the job is
        permanently due, retried every tick forever, and `period` decides
        nothing. ADR-0046's decision-2 table gets this wrong in its **second
        row**, the `search_queries` retention one -- which is also the only
        registration that has shipped, the neighbour rebuild being J6. It
        tabulates `min(search_queries.at)` as retention's `last_done()` and
        calls it *"exact"*, but that is the age of the **oldest surviving
        row**, written by the search path. After a prune it sits at the
        retention window's age and stays there, because rows keep ageing into
        the window. Measured 2026-08-27 on `usher_j2`, a clone of this
        deployment's catalog: `min(at)` was 14 days old over 14,978 rows, which
        reads *due* against any period shorter than the window.

        ✅ **J5 discharged this, and the shipped reading is
        `min(min(at) + window, now)`** --
        `usher.services.scheduler.SearchQueryRetention.last_done`, which
        carries the argument. The artefact a prune maintains is the table's
        *lower bound*, not a row: a run at instant *T* establishes "no row is
        older than *T* - window", so the invariant held at *T* and goes on
        holding until the oldest surviving row itself falls out of the window.
        This job's own runs move it and a new search cannot, because a new row
        is the newest one; and an **empty** table answers `now` rather than
        `None`, since nothing to prune is the invariant satisfied. The
        obligation stays stated here because it is what the *next*
        registration owes, and this docstring said **"J5 owes a real completion
        time or a different design"** until 2026-09-07, at a HEAD where J5 had
        shipped one.

        🔴 **An artefact built incrementally must answer for its *oldest*
        part, and that choice costs a period.** `SimilarityService.computed_at()`
        is `min(computed_at)` and not `max` for the reason ADR-0046 gives: the
        newest row reports a whole-table rebuild as fresh the moment its first
        page commits, so a scheduler reading `max` would start a
        three-and-a-half-hour walk and have no way to tell a finished walk from
        a started one. The price, which that record does not state: at the
        instant a walk *finishes*, `min` already answers the walk's own
        duration earlier. Measured on this deployment's last completed walk
        (2026-08-27, `max(computed_at) - min(computed_at)` on `title_neighbors`):
        **3 h 20 m 33 s**. So a declared period *P* behaves as *P* minus the
        walk, and **at any *P* at or under 3.34 h the job is due the moment it
        completes and runs back to back forever**. A registration's period has
        to clear its own artefact's build time with room to spare; any job
        registered here owes both this argument and the `min`/`max` one about
        its own artefact.

        This is a database read on the shipped registrations, so it may raise
        a `UsherPortError`. The scheduler counts that as a failure of this job
        and does not run it -- starting a multi-hour batch on the strength of
        a read that did not answer is the one thing worse than not running it.
        """

    @abstractmethod
    async def run(self) -> JobOutcome:
        """Do the work, once.

        Answers `DONE`, or `DECLINED` for work this deployment's state makes
        pointless to attempt -- `JobOutcome` carries what each costs.

        **Must be safe to cancel at any `await`**: `Scheduler.stop()` cancels
        the loop task, so an in-flight run is cancelled wherever it happens to
        be suspended. **And safe to run twice**: the scheduler holds no lock,
        `USHER_SCHEDULER_ENABLED` is per process, and an operator who turns it
        on in two places gets two runners (ADR-0046, decision 3).

        A raise is logged with `name` and counted on
        `usher.scheduler.job.failures`; it stops neither the tick nor the
        loop, and the next tick will find the job still due because the
        artefact did not move.
        """


__all__ = ["JobOutcome", "ScheduledJob"]
