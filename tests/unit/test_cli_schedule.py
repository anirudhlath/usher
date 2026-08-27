"""`usher schedule` and `usher schedule --once` (ADR-0046, M10's J4).

**The `_dispatch` arm is the case this file exists for.** `_dispatch`'s `else`
is `serve`, so a subcommand that parses and has no arm of its own does not
fail -- it silently starts the HTTP server, and looks like it worked because
the server does start. `tests/unit/test_cli_errors.py`'s CLI-wide boundary
sweep cannot see that: it makes every dispatch coroutine *and* `uvicorn.run`
raise the identical exception on purpose, which is exactly what makes the two
arms indistinguishable there. `.claude/rules/config-cli-and-deployment.md`
records it as a standing debt a new command owes.

Nothing here opens a database. At this commit `_schedule` genuinely does not
need one -- the registry ships empty (`composition.build_scheduler`) and a tick
over zero jobs asks nothing -- and the `--once` case below is what pins that,
because a `_schedule` that built an engine would fail against the unreachable
DSN these settings carry.
"""

from datetime import UTC, datetime, timedelta

import pytest

from usher.cli import build_parser, main
from usher.config import Settings
from usher.ports.scheduler import ScheduledJob
from usher.services.scheduler import Scheduler


def test_schedule_is_advertised_by_the_parser_in_both_forms() -> None:
    """A subcommand `build_parser` does not declare is a command
    `test_cli_errors.py`'s boundary sweep never runs -- and `--once` is the
    half an operator's crontab calls, so it is the half most worth having a
    parser assertion of its own."""
    daemon = build_parser().parse_args(["schedule"])
    assert daemon.command == "schedule"
    assert daemon.once is False

    once = build_parser().parse_args(["schedule", "--once"])
    assert once.command == "schedule"
    assert once.once is True


def test_schedule_mirrors_works_argument_surface() -> None:
    """`usher work` / `usher work --once` is the shape this deliberately
    copies, so the two are asserted to carry the same argument rather than
    left to look similar. A second flag added to one and not the other is what
    this fails on."""
    schedule = vars(build_parser().parse_args(["schedule"]))
    work = vars(build_parser().parse_args(["work"]))
    assert set(schedule) == set(work) == {"command", "traceback", "once"}


def test_schedule_dispatches_to_the_scheduler_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`_dispatch`'s `else` arm is `serve`.** So the two are made to differ:
    `_schedule` records, the server raises.

    Both forms, because `--once` and the daemon reach the same arm through the
    same `args.once` and a dispatch that ignored the flag would still record.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    ran: list[bool] = []

    async def _record(settings: object, *, once: bool) -> None:
        ran.append(once)

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher schedule started the HTTP server")

    monkeypatch.setattr("usher.cli._schedule", _record)
    monkeypatch.setattr("uvicorn.run", _served)

    main(["schedule", "--once"])
    main(["schedule"])

    assert ran == [True, False]


def test_one_tick_over_an_empty_registry_says_so_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real `_schedule`, end to end, against the state this commit ships.

    **Both numbers are printed and the second is the load-bearing one.** `ran`
    alone cannot distinguish *"nothing was due"* from *"nothing is
    registered"*, and at this commit the second is the shipped state -- so a
    line carrying only the first would read as a healthy night on a deployment
    where the scheduler can never do anything at all. J5 and J6 are what move
    the denominator off zero.

    That this returns at all against an unreachable database is the other
    half: `_schedule` opens no connection while the registry is empty.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher schedule --once started the HTTP server")

    monkeypatch.setattr("uvicorn.run", _served)

    main(["schedule", "--once"])

    # A whole line, not a substring: `main` also calls `configure_telemetry`,
    # whose serialised loguru sink shares stdout, so an equality against the
    # entire capture would be an assertion about logging.
    assert "0 of 0 scheduled jobs ran" in capsys.readouterr().out.splitlines()


class _Recent(ScheduledJob):
    """A job whose artefact was rebuilt an hour ago, against a daily period."""

    name = "recent"
    period = timedelta(days=1)

    def __init__(self) -> None:
        self.runs = 0

    async def last_done(self) -> datetime | None:
        return datetime.now(UTC) - timedelta(hours=1)

    async def run(self) -> None:
        self.runs += 1


def test_one_tick_does_not_run_a_job_whose_period_has_not_elapsed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 **The decision ADR-0046 does not state, made here and pinned here.**

    That record sells `usher schedule --once` as the answer for a wall-clock
    schedule -- an operator's 3am cron -- without saying whether the period
    still gates the tick. **It does.** A `--once` that ignored it would turn a
    crontab line into an unconditional *"start the three-and-a-half-hour
    rebuild now"*, and would give one command two meanings depending on a flag.

    The consequence, which is a real limit rather than a footnote:
    *"every night at 3am"* only happens if the job's period is comfortably
    under a day, and a period is a property of the **job**, not a setting.
    `usher similar --rebuild` remains the command that runs a batch
    unconditionally.

    The reported line is the control: `1` registered and `0` run is what makes
    this a statement about the period rather than about an empty registry,
    which is the state every other case in this file runs against.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    job = _Recent()

    def _build(settings: Settings) -> Scheduler:
        scheduler = Scheduler(tick_seconds=settings.scheduler_tick_seconds)
        scheduler.register(job)
        return scheduler

    monkeypatch.setattr("usher.cli.build_scheduler", _build)

    main(["schedule", "--once"])

    assert job.runs == 0, "`--once` ran a job whose period had not elapsed"
    assert "0 of 1 scheduled jobs ran" in capsys.readouterr().out.splitlines()
