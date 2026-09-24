"""`usher schedule` and `usher schedule --once`."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.commands import configured, dispatched
from usher.cli import build_parser, main
from usher.config import Settings
from usher.ports.scheduler import JobOutcome, ScheduledJob
from usher.services.scheduler import Scheduler


def test_schedule_is_advertised_by_the_parser_in_both_forms() -> None:
    """`build_parser` declares `schedule` in both forms; a subcommand it omits never runs."""
    daemon = build_parser().parse_args(["schedule"])
    assert daemon.command == "schedule"
    assert daemon.once is False

    once = build_parser().parse_args(["schedule", "--once"])
    assert once.command == "schedule"
    assert once.once is True


def test_schedule_mirrors_works_argument_surface() -> None:
    """`schedule` carries the same arguments as `work`, so a flag added to one fails here."""
    schedule = vars(build_parser().parse_args(["schedule"]))
    work = vars(build_parser().parse_args(["work"]))
    assert set(schedule) == set(work) == {"command", "traceback", "once"}


def test_schedule_dispatches_to_the_scheduler_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both forms reach `_schedule`, each carrying its own `args.once`."""
    configured(monkeypatch)

    once = dispatched(monkeypatch, arm="_schedule", argv=["schedule", "--once"])
    daemon = dispatched(monkeypatch, arm="_schedule", argv=["schedule"])

    assert once + daemon == [{"once": True}, {"once": False}]


def test_one_tick_over_an_empty_registry_says_so_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One tick over an empty registry prints both numbers and returns.

    `ran` alone cannot distinguish *"nothing was due"* from *"nothing is
    registered"*, and `_schedule` builds an engine without opening a
    connection, so an unreachable database still reaches the line.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher schedule --once started the HTTP server")

    monkeypatch.setattr("uvicorn.run", _served)
    # The registry is substituted rather than shipped: this case is about the
    # *line* -- both numbers, and `0 of 0` being sayable at all -- which is a
    # property of `_schedule`, not of what happens to be registered today.
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

    async def run(self) -> JobOutcome:
        self.runs += 1
        return JobOutcome.DONE


def test_one_tick_does_not_run_a_job_whose_period_has_not_elapsed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--once` still gates on the job's period rather than running unconditionally.

    A `--once` that ignored the period would turn an operator's 3am crontab line
    into *"start the rebuild now"*. The reported `0 of 1` is the control: one job
    registered, none run.
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
