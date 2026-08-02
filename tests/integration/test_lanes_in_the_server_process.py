"""The lanes, running inside a real `create_app()` against real Postgres.

**This file exists because "the server process grows two lanes" is a claim
about a process, and no unit test can make it.** `tests/unit/test_api_lanes.py`
drives `LaneSupervisor` directly over port fakes, which proves the supervisor
does what it is told; it says nothing about whether `create_app`'s lifespan
tells it anything. So the headline case here starts nothing but the app,
runs no `usher work`, and asserts a real row in `jobs` disappears.

The three things a fake cannot express, all here:

- **The worker lane claims from the real queue.** `FakeJobQueue` has no
  `FOR UPDATE SKIP LOCKED`, no `clock_timestamp()`, and no transaction --
  and the lane opens one session per pass, which is exactly the shape a
  rolled-back single-transaction fixture cannot model.
- **`_write_push_available` writing nothing is invisible to a fake.**
  `sources` has a `BEFORE UPDATE` trigger that owns `updated_at`, so an
  unconditional `UPDATE` moves a column an operator reads. `now()` is frozen
  per transaction and each call here opens its own, so the two instants
  really are different and a no-op write is observable.
- **A push lane against a source row.** The lane's source list, credential
  decryption and adapter build all go through the real repositories.

The adapter itself is a fake, deliberately and by necessity: a real one
would open a socket to a media server, and no test in this repository makes
a network request. `dependency_overrides` do not reach the lifespan, so the
substitution is made where a composition root makes it -- in the unit of
work handed to `LaneSupervisor`.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.source_adapter import FakeSourceAdapter
from usher.api.app import create_app
from usher.api.lanes import LaneSupervisor
from usher.composition import DefaultUserId, Pipeline, build_pipeline, unit_of_work
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.source import PostgresSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.jobs import JobRequest
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.events import InMemoryEventBus

SECRET_KEY = "0123456789abcdef0123456789abcdef"
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
# Bounded, because the failure this file is guarding against ("the lane
# never ran") is otherwise a hang rather than a failure -- and
# `asyncio.wait_for` cannot bound a poll loop that keeps yielding. Generous
# against `IDLE_SLEEP_SECONDS = 5.0`: the first pass is immediate, so a
# working lane finishes in milliseconds and only a broken one waits.
BOUND_SECONDS = 20.0


@pytest.fixture
def lane_settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # The point of this file. Push stays off in the headline case --
        # there is no source row, so a push lane would have nothing to do
        # anyway -- and is turned on explicitly by the case that wants one.
        worker_enabled=True,
        push_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real, separately-committing sessions -- not the suite's usual
    rolled-back one.

    The lane under test commits for real from another task, so a test that
    wanted to see its writes through a single shared transaction would see
    nothing. Each case therefore cleans up after itself; `jobs` and `titles`
    do not cascade from anything (CLAUDE.md's "a route-driven test commits
    for real").
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Undo what a committing test wrote, including the staging tables.

    `usher.db.staging` creates `stg_*` with DDL and Postgres DDL is
    transactional, so a *committing* test is the only kind that can leak
    one -- and it surfaces as schema drift in
    `test_migrations.py::test_migration_matches_the_orm_metadata`, a
    different file that then fails only in combination. Reproduced here
    exactly as CLAUDE.md predicts: without the two `DROP`s below, this file
    passes alone and takes the migration test down in the full run.
    """
    async with sessions() as session:
        for statement in (
            "DELETE FROM jobs",
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM sources",
            "DROP TABLE IF EXISTS stg_jobs",
            "DROP TABLE IF EXISTS stg_media_items",
        ):
            await session.execute(text(statement))
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


async def _queue_depth(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        count = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
    return int(count)


async def test_the_worker_lane_drains_the_queue_inside_the_server_process(
    lane_settings: Settings, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """**The milestone's central claim, proved rather than asserted.**

    A real `match` job goes into a real `jobs` table, an app is started with
    nothing but `LifespanManager`, and the row is gone before the app stops.
    Nothing in this test claims a job, registers a handler, or runs `usher
    work` -- the only thing between the enqueue and the empty table is
    `create_app`'s lifespan.

    The key names an item no configured source addresses, so
    `SourceRegistry.resolve` answers `None` from local state alone and the
    handler completes the job without a network call (PRD 08 reserves
    parking for work a human has to look at).
    """
    async with sessions() as session:
        pipeline = build_pipeline(session, lane_settings)
        await pipeline.queue.enqueue(
            [JobRequest(kind=JobKind.MATCH, key="nobody-addresses-this", priority=JobPriority.NEW)]
        )
        await session.commit()
    assert await _queue_depth(sessions) == 1

    app = create_app(lane_settings)
    async with LifespanManager(app):
        lanes = app.state.lanes
        assert isinstance(lanes, LaneSupervisor)
        assert lanes.worker_running() is True
        deadline = time.perf_counter() + BOUND_SECONDS
        while await _queue_depth(sessions) and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
    assert await _queue_depth(sessions) == 0, (
        "the job survived a whole app lifetime; the worker lane is not running "
        "in the server process"
    )
    # And the lane stops with the app, rather than outliving the lifespan
    # and polling a disposed engine.
    assert app.state.lanes.worker_running() is False


async def test_the_worker_lane_is_off_when_the_setting_is(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """PRD 01's `--worker` flag, as configuration: the same image with the
    switch off leaves the queue for another container.

    The mirror of the case above and the reason it is evidence -- without
    this, "the job disappeared" could be anything in the process."""
    async with sessions() as session:
        pipeline = build_pipeline(
            session, Settings(database_url=postgres_url, secret_key=SECRET_KEY)
        )
        await pipeline.queue.enqueue(
            [JobRequest(kind=JobKind.MATCH, key="nobody-addresses-this", priority=JobPriority.NEW)]
        )
        await session.commit()

    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key=SECRET_KEY,
            worker_enabled=False,
            push_enabled=False,
        )
    )
    async with LifespanManager(app):
        assert app.state.lanes.worker_running() is False
        await asyncio.sleep(0.2)
        assert await _queue_depth(sessions) == 1


class _Adapters(SourceAdapterFactory):
    def __init__(self) -> None:
        self.built: list[FakeSourceAdapter] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        adapter = FakeSourceAdapter(source)
        self.built.append(adapter)
        return adapter


def _with_fake_adapters(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, adapters: _Adapters
) -> object:
    """The real unit of work with one field swapped.

    `Pipeline` is a frozen dataclass, so `dataclasses.replace` is the write
    path -- the `.evolve()` rule is about `usher.domain`'s `DomainModel`
    subclasses and does not reach a composition-root DTO. Everything else in
    the pipeline is the real thing on a real session.
    """
    real = unit_of_work(sessions, settings, events=InMemoryEventBus())

    @asynccontextmanager
    async def swapped() -> AsyncIterator[Pipeline]:
        async with real() as pipeline:
            yield replace(pipeline, adapters=adapters)

    return swapped


async def test_a_push_lane_starts_for_a_real_source_row(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """The lane's source list, credential decryption and adapter build, all
    through the real repositories against real rows.

    A fake `SourceRepository` cannot express the one thing that has ever
    gone wrong here -- an encrypted credential that does not decrypt under
    the configured `USHER_SECRET_KEY` -- and `PostgresCredentialStore` is
    the only implementation that can.
    """
    settings = Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        worker_enabled=False,
        push_enabled=True,
    )
    source = Source(
        kind=SourceKind.EMBY,
        name="Lane Emby",
        base_url="https://lane.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        await PostgresCredentialStore(session, settings.secret_key).put(
            source.credentials_ref, CREDENTIALS, owner_id=source.id
        )
        await session.commit()

    supervisor = LaneSupervisor(
        settings,
        _with_fake_adapters(sessions, settings, adapters := _Adapters()),  # type: ignore[arg-type]
        InMemoryEventBus(),
        user_id=DefaultUserId(sessions),
    )
    await supervisor.start()
    try:
        deadline = time.perf_counter() + BOUND_SECONDS
        while not supervisor.running_sources() and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert supervisor.running_sources() == ["Lane Emby"]
        assert len(adapters.built) == 1
        # PRD 10's two series, read off the running lane's own adapter --
        # which is what makes `usher.source.push.connected` a series a
        # deployment emits rather than an instrument nothing feeds.
        snapshots = supervisor.push_snapshots()
        assert set(snapshots) == {"Lane Emby"}
        assert snapshots["Lane Emby"].reconnects == 0
        assert supervisor.push_available(source.id) is not None
    finally:
        await supervisor.stop()
    assert supervisor.running_sources() == []
    assert adapters.built[0]._closed is True


async def test_writing_the_push_availability_it_already_has_writes_nothing(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """`sources` has a `BEFORE UPDATE` trigger that owns `updated_at`, so an
    unconditional `UPDATE` moves a column an operator reads to see when a
    source last changed -- once per reconnect of a flapping lane.

    Only real Postgres can catch this: a fake repository has no trigger, so
    a no-op write is a no-op there whatever the code does. Two separate
    transactions, because `now()` is `transaction_timestamp()` and is frozen
    for the life of one -- inside a single transaction the *unconditional*
    version would also read back an unchanged instant and the case would
    ratify it.
    """
    settings = Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        worker_enabled=False,
        push_enabled=False,
    )
    source = Source(
        kind=SourceKind.EMBY,
        name="Quiet Emby",
        base_url="https://quiet.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
        supports_push=False,
    )
    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        await session.commit()

    async def stamp() -> object:
        async with sessions() as session:
            return (
                await session.execute(
                    text("SELECT updated_at FROM sources WHERE id = :id"), {"id": source.id}
                )
            ).scalar_one()

    supervisor = LaneSupervisor(
        settings,
        unit_of_work(sessions, settings, events=InMemoryEventBus()),
        InMemoryEventBus(),
        user_id=DefaultUserId(sessions),
    )
    before = await stamp()
    await supervisor._write_push_available(source, False)
    assert await stamp() == before, "an unchanged value still wrote a row"

    # And a real change still writes, so the guard is not "never write".
    await supervisor._write_push_available(source, True)
    after = await stamp()
    assert after != before
    async with sessions() as session:
        stored = await PostgresSourceRepository(session).get(source.id)
    assert stored is not None
    assert stored.supports_push is True
