"""`usher schedule` and `usher schedule --once` (ADR-0046, M10's J4).

**The `_dispatch` arm is the case this file exists for.** `_dispatch`'s `else`
is `serve`, so a subcommand that parses and has no arm of its own does not
fail -- it silently starts the HTTP server, and looks like it worked because
the server does start. `tests/unit/test_cli_errors.py`'s CLI-wide boundary
sweep cannot see that: it makes every dispatch coroutine *and* `uvicorn.run`
raise the identical exception on purpose, which is exactly what makes the two
arms indistinguishable there. `.claude/rules/config-cli-and-deployment.md`
records it as a standing debt a new command owes.

**Nothing here opens a connection, and since J5 that is a property rather than
an accident.** `_schedule` now builds an engine -- the retention registration
needs a session factory -- and `build_engine` connects to nothing, so every
case below runs against an unreachable DSN and returns. A `_schedule` that
opened a connection eagerly would fail all of them, which is what makes
`create_app`'s own *"a lane connects to nothing at start"* property assertable
for this command too.

The registry these cases drive is a **substituted** one, because what they are
about is `--once`'s arithmetic rather than which jobs a deployment runs; the
real registry is `tests/unit/test_services_scheduler.py`'s subject.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.commands import configured, dispatched
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
    """Both forms, because `--once` and the daemon reach the same arm through
    the same `args.once` and a dispatch that ignored the flag would still
    record."""
    configured(monkeypatch)

    once = dispatched(monkeypatch, arm="_schedule", argv=["schedule", "--once"])
    daemon = dispatched(monkeypatch, arm="_schedule", argv=["schedule"])

    assert once + daemon == [{"once": True}, {"once": False}]


def test_one_tick_over_an_empty_registry_says_so_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real `_schedule`, end to end, against the state this commit ships.

    **Both numbers are printed and the second is the load-bearing one.** `ran`
    alone cannot distinguish *"nothing was due"* from *"nothing is
    registered"*, and the second was the shipped state for one commit -- so a
    line carrying only the first would have read as a healthy night on a
    deployment where the scheduler could never do anything at all. J5 moved
    the denominator to 1 and J6 moves it to 2; an empty registry is still
    reachable, and is what a process with no way to a database gets.

    That this returns at all against an unreachable database is the other
    half: `_schedule` builds an engine and opens no connection.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher schedule --once started the HTTP server")

    monkeypatch.setattr("uvicorn.run", _served)
    # The empty registry is substituted rather than shipped since J5. What this
    # case is about is the *line* -- both numbers, and `0 of 0` being sayable
    # at all -- which is a property of `_schedule` and not of what happens to
    # be registered today.
    monkeypatch.setattr(
        "usher.cli.build_scheduler",
        lambda settings, *, sessions: Scheduler(tick_seconds=settings.scheduler_tick_seconds),
    )

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

    def _build(settings: Settings, *, sessions: object) -> Scheduler:
        scheduler = Scheduler(tick_seconds=settings.scheduler_tick_seconds)
        scheduler.register(job)
        return scheduler

    monkeypatch.setattr("usher.cli.build_scheduler", _build)

    main(["schedule", "--once"])

    assert job.runs == 0, "`--once` ran a job whose period had not elapsed"
    assert "0 of 1 scheduled jobs ran" in capsys.readouterr().out.splitlines()
