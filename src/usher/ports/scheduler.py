"""The scheduled-work port: nothing runs `usher similar --rebuild` for you."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import StrEnum


class JobOutcome(StrEnum):
    """What one `ScheduledJob.run()` amounted to, and what the loop does with each.

    `DECLINED` is a run that never started, because the deployment is in a
    state no retry fixes. Neither work nor failure, so counted in neither
    place: a refusal's milliseconds in `usher.scheduler.job.duration` would
    read as a very fast run of a job that takes hours, and
    `usher.scheduler.job.failures` is for a job that tried and broke. It does
    get a failure's spacing, which stops the refusal being logged on every
    tick for as long as an operator leaves it.
    """

    DONE = "done"
    DECLINED = "declined"


class ScheduledJob(ABC):
    """One named batch the scheduler may run on a period."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable, and used as a metric label and a span name.

        `usher.scheduler.job.duration`, `.failures` and `.due` are labelled
        `job` with this string and the run's span is `scheduler.<name>`, so
        renaming one empties a panel and splits a histogram in two. Jobs may
        not share it; `Scheduler.register` refuses rather than letting one
        label cover two populations.
        """

    @property
    @abstractmethod
    def period(self) -> timedelta:
        """The **minimum interval since the last completion**, never a wall-clock schedule.

        "Every night at 3am" is not expressible here and is not offered: an
        operator wanting that runs `usher schedule --once` from their own
        cron, which stays a supported path.

        A job is due when `now - last_done() >= period`, so one hour means at
        least an hour has passed, not more than an hour. Compared per tick
        rather than cached as a due-time, because another process may have
        moved `last_done()`.
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

        Must be safe to cancel at any `await`: `Scheduler.stop()` cancels the
        loop task wherever an in-flight run is suspended. And safe to run
        twice: the scheduler holds no lock, `USHER_SCHEDULER_ENABLED` is per
        process, and an operator who turns it on in two places gets two
        runners.

        A raise is logged with `name` and counted on
        `usher.scheduler.job.failures`; it stops neither the tick nor the
        loop, and the next tick will find the job still due because the
        artefact did not move.
        """


__all__ = ["JobOutcome", "ScheduledJob"]
