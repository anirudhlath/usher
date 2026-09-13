"""The scheduled-work port (ADR-0046.

and PRD 08's "nothing runs `usher similar --rebuild` for you").
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import StrEnum


class JobOutcome(StrEnum):
    """What one `ScheduledJob.run()` amounted to, and what the loop does with each.

    **The one statement of this; every other site points here.**

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
        """The **minimum interval since the last completion**, never a wall-clock schedule.

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
        """When this job's **artefact** was last complete, or `None` if it has never been built.

        Timezone-aware.
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
