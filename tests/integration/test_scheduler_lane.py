"""The scheduled-work lane, running inside a real `create_app()`."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.metadata_provider import FakeMetadataProvider
from usher.api.app import create_app
from usher.api.lanes import LaneSupervisor
from usher.composition import Pipeline
from usher.config import Settings
from usher.ports.events import NullEventPublisher
from usher.ports.scheduler import JobOutcome, ScheduledJob
from usher.services.scheduler import Scheduler

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Bounded, because the failure this file guards against ("the lane never ran")
# is otherwise a hang rather than a failure. Generous against a working lane,
# which ticks immediately and finishes in milliseconds -- only a broken one
# waits.
BOUND_SECONDS = 20.0


class _OneShot(ScheduledJob):
    """A job that has never been built, so it is due on the very first tick.

    **The artefact is this object**, which is what makes `runs == 1` an
    assertion rather than a race: `last_done()` answers `None` until the run,
    and a real instant afterwards, so a second tick inside the same lifespan
    finds it not due against a year-long period. A job whose `last_done()`
    stayed `None` would be due forever and the count would be whatever the
    scheduler's tick rate and the case's deadline happened to produce.

    `name` and `period` are plain class attributes, which is the whole point
    of the port declaring them as abstract properties: the enforcement costs an
    implementation nothing.
    """

    name = "test.one-shot"
    period = timedelta(days=365)

    def __init__(self) -> None:
        self.ran = asyncio.Event()
        self.runs = 0
        self._done: datetime | None = None

    async def last_done(self) -> datetime | None:
        return self._done

    async def run(self) -> JobOutcome:
        self.runs += 1
        self._done = datetime.now(UTC)
        self.ran.set()
        return JobOutcome.DONE


def _settings(postgres_url: str, *, scheduler: bool) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # The other two lanes are off: this file is about the third, and a
        # worker polling the real queue or a push lane opening a socket would
        # be noise in a case whose whole subject is one job running.
        worker_enabled=False,
        push_enabled=False,
        scheduler_enabled=scheduler,
    )


def _with_job(monkeypatch: pytest.MonkeyPatch, job: ScheduledJob) -> None:
    """The real registry, replaced by one holding a single fake.

    ⚠️ **`sessions`, `client`, `metadata` and `user_id` are accepted and dropped**,
    which is what keeps the substitution honest: `LaneSupervisor.start` passes what
    `create_app` handed it, so a supervisor that stopped passing one would be
    a `TypeError` here rather than a silent narrowing. What the fake must not
    do is *use* them -- these cases are about the lane, and the real
    registration's database work is
    `tests/integration/test_search_query_retention.py`'s subject.
    """

    def _build(
        settings: Settings, *, sessions: object, client: object, metadata: object, user_id: object
    ) -> Scheduler:
        scheduler = Scheduler(tick_seconds=settings.scheduler_tick_seconds)
        scheduler.register(job)
        return scheduler

    monkeypatch.setattr("usher.api.lanes.build_scheduler", _build)


async def test_the_scheduler_lane_runs_a_due_job_inside_the_server_process(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job running, not an assertion about wiring.

    Nothing here creates a task, calls `tick()`, or runs `usher schedule` -- the
    only thing between the registration and the run is `create_app`'s lifespan.
    The premise is asserted before the wait, because "the job never ran" and
    "the lane was never started" are different failures the deadline below
    cannot tell apart.
    """
    job = _OneShot()
    _with_job(monkeypatch, job)

    app = create_app(_settings(postgres_url, scheduler=True))
    async with LifespanManager(app):
        lanes = app.state.lanes
        assert isinstance(lanes, LaneSupervisor)
        assert lanes.scheduler_running() is True, "the lifespan started no scheduler lane"
        deadline = time.perf_counter() + BOUND_SECONDS
        while not job.ran.is_set() and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)

    assert job.runs == 1, (
        "a due job survived a whole app lifetime unrun; the scheduler lane is not "
        "running in the server process"
    )
    # And the lane stops with the app rather than outliving the lifespan and
    # ticking against a disposed engine.
    assert app.state.lanes.scheduler_running() is False


async def test_the_scheduler_lane_is_off_when_the_setting_is(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job stays unrun when the setting is off.

    `USHER_SCHEDULER_ENABLED=false` is the shipped default, so this is what
    every deployment does until an operator decides otherwise -- and it keeps a
    fresh install from starting a multi-hour rebuild over an empty table.
    """
    settings = _settings(postgres_url, scheduler=False)
    assert settings.scheduler_enabled is False, "the premise: off is the shipped default"
    job = _OneShot()
    _with_job(monkeypatch, job)

    app = create_app(settings)
    async with LifespanManager(app):
        assert app.state.lanes.scheduler_running() is False
        # Long enough for the first tick to have happened if it were going to:
        # the loop ticks before its first sleep.
        await asyncio.sleep(0.2)

    assert job.runs == 0, "the scheduler ran a job in a process configured not to schedule"


async def test_the_supervisor_hands_the_scheduler_its_own_client_provider_and_household(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_scheduler` receives the very objects the supervisor was constructed with.

    The seed-row job is registered only where a client, a session factory and a household
    reader are all present, so a supervisor that passed `None` for one would register
    nothing and every other case in this file would still pass. Identity, not equality:
    a client the supervisor rebuilt would be a second one in the same process.

    No database: `start()` connects to nothing, and the worker and push lanes are off.
    """
    client, provider = FakeLLMClient(), FakeMetadataProvider()
    sessions = async_sessionmaker[AsyncSession]()

    async def household() -> uuid.UUID:
        return uuid.uuid4()

    @asynccontextmanager
    async def no_work() -> AsyncIterator[Pipeline]:
        raise AssertionError("a scheduler-only supervisor opened a unit of work")
        yield  # pragma: no cover

    handed: list[dict[str, object]] = []

    def _build(settings: Settings, **held: object) -> Scheduler:
        handed.append(held)
        return Scheduler(tick_seconds=settings.scheduler_tick_seconds)

    monkeypatch.setattr("usher.api.lanes.build_scheduler", _build)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key=SECRET_KEY,
        worker_enabled=False,
        push_enabled=False,
        scheduler_enabled=True,
    )
    lanes = LaneSupervisor(
        settings,
        no_work,
        NullEventPublisher(),
        user_id=household,
        provider=provider,
        client=client,
        sessions=sessions,
    )

    await lanes.start()
    try:
        assert lanes.scheduler_running() is True, "the premise: the scheduler lane started"
        assert len(handed) == 1, "the supervisor built the scheduler other than once"
        assert handed[0].get("client") is client
        assert handed[0].get("metadata") is provider
        assert handed[0].get("user_id") is household
        assert handed[0].get("sessions") is sessions
    finally:
        await lanes.stop()
