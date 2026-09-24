"""The lanes, running inside a real `create_app()` against real Postgres."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
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
# The shape every `(thing, close it)` pair in `usher.composition` returns.
AsyncCloser = Callable[[], Awaitable[None]]


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


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Undo what a committing test wrote.

    The staging tables are `CREATE TEMP TABLE ... ON COMMIT DROP`, so the commit is what
    removes them and nothing here has to. A leaked one surfaces as schema drift in
    `test_migrations.py`, a different file that then fails only in combination.
    """
    async with sessions() as session:
        for statement in (
            "DELETE FROM jobs",
            # The cost ledger, which cascades from nothing: it has no
            # `user_id` at all (`generation_id` is its only correlation key,
            # which is what makes PRD 10's dashboard 5 a join rather than a
            # lookup), so a committing curate case has to clean it up itself.
            "DELETE FROM llm_calls",
            "DELETE FROM users WHERE name = 'default'",
            "DELETE FROM sources",
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
    """The central claim of this file, proved rather than asserted.

    A real `match` job goes into a real `jobs` table, an app is started with
    nothing but `LifespanManager`, and the row is gone before the app stops.
    Nothing in this test claims a job, registers a handler, or runs `usher
    work` -- the only thing between the enqueue and the empty table is
    `create_app`'s lifespan.

    The key names an item no configured source addresses, so
    `SourceRegistry.resolve` answers `None` from local state alone and the
    handler completes the job without a network call (PRD 08: a job no
    configured source addresses completes, and does not park).
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
    """PRD 01's split into a second container, as configuration.

    The same image with the switch off leaves the queue for another container. The
    mirror of the case above and the reason it is evidence -- without this, "the job
    disappeared" could be anything in the process.
    """
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


async def _curate_status(sessions: async_sessionmaker[AsyncSession], key: str) -> str | None:
    async with sessions() as session:
        return (
            await session.execute(
                text("SELECT status FROM jobs WHERE kind = 'curate' AND key = :key"), {"key": key}
            )
        ).scalar_one_or_none()


async def test_a_curate_job_parks_in_the_server_process_when_there_is_nothing_to_curate(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """The wiring `create_app` has that no unit test can see.

    `tests/unit/test_api_lanes.py` proves a `LaneSupervisor` *given* an `LLMClient`
    claims curate work, and says nothing about whether the lifespan ever builds one.
    """
    settings = Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        worker_enabled=True,
        push_enabled=False,
        llm_enabled=True,
    )
    household = str(new_id())
    async with sessions() as session:
        await build_pipeline(session, settings).queue.enqueue(
            [JobRequest(kind=JobKind.CURATE, key=household, priority=JobPriority.BACKFILL)]
        )
        await session.commit()

    async with LifespanManager(create_app(settings)):
        deadline = time.perf_counter() + BOUND_SECONDS
        while (
            await _curate_status(sessions, household)
        ) != "parked" and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)

    assert await _curate_status(sessions, household) == "parked", (
        "the curate job was never claimed and parked; the server process built no LLM client"
    )
    # The whole table, not a household's rows: `llm_calls` carries no
    # `user_id`, and the `clean` fixture empties it either side of this case.
    async with sessions() as session:
        billed = (await session.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one()
    assert int(billed) == 0, "an empty catalog was billed for a completion"


async def test_a_curate_job_waits_for_a_process_that_has_a_model(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """The mirror, and the reason the case above is evidence.

    Without it, "the job parked" could be anything in the process.

    `USHER_LLM_ENABLED=false` is the shipped default, so this is what nearly
    every deployment does with a curate job -- it leaves it `pending` for a
    process that can run it. Parking it instead would fill PRD 08's review
    list with work whose only problem was the process it was offered to, and
    a parked job needs a human to release it. Same bargain `index` takes on a
    deployment without the embedding extra.
    """
    settings = Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        worker_enabled=True,
        push_enabled=False,
    )
    assert settings.llm_enabled is False, "the premise: off is the shipped default"
    household = str(new_id())
    async with sessions() as session:
        await build_pipeline(session, settings).queue.enqueue(
            [JobRequest(kind=JobKind.CURATE, key=household, priority=JobPriority.BACKFILL)]
        )
        await session.commit()

    async with LifespanManager(create_app(settings)):
        # Long enough for several passes at the lane's own floor to have
        # claimed it if it were going to: the first pass is immediate.
        await asyncio.sleep(0.2)

    assert await _curate_status(sessions, household) == "pending"


class _Closes:
    """Counts what a composition root actually released.

    A `(thing, close it)` pair whose `close it` is never called is the one defect
    neither a fake nor `mypy` can see: the object is built, the process works, and the
    transport leaks.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def factory(self, name: str) -> Callable[..., Awaitable[tuple[None, AsyncCloser]]]:
        async def _build(*_: object, **__: object) -> tuple[None, AsyncCloser]:
            async def _close() -> None:
                self.calls.append(name)

            return None, _close

        return _build


async def test_the_lifespan_releases_every_process_resource_it_built(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, clean: None
) -> None:
    """`create_app`'s `finally` is asserted rather than read."""
    closes = _Closes()
    monkeypatch.setattr("usher.api.app.metadata_provider", closes.factory("provider"))
    monkeypatch.setattr("usher.api.app.embedder", closes.factory("embedder"))
    monkeypatch.setattr("usher.api.app.llm_client", closes.factory("client"))
    settings = Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        worker_enabled=True,
        push_enabled=False,
    )

    async with LifespanManager(create_app(settings)):
        assert closes.calls == [], "a resource was released while the process was still up"

    assert sorted(closes.calls) == ["client", "embedder", "provider"], (
        "the lifespan built three process-lifetime resources and did not release all three"
    )


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
    """The lane's source list, credential decryption and adapter build.

    All through the real repositories against real rows. A fake `SourceRepository`
    cannot express the one thing that has ever gone wrong here -- an encrypted
    credential that does not decrypt under the configured `USHER_SECRET_KEY` -- and
    `PostgresCredentialStore` is the only implementation that can.
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
    """`sources` has a `BEFORE UPDATE` trigger that owns `updated_at`.

    So a lane that wrote unconditionally would move a column an operator reads to see
    when a source last changed, once per reconnect of a flapping socket.

    **The guard is not what prevents that today.** Deleting `_write_push_available`'s
    equality check leaves this case green: `PostgresSourceRepository.update` sets
    attributes on a *loaded ORM row* and SQLAlchemy's unit of work emits no `UPDATE`
    when no attribute actually changed, so the trigger never fires either way. The guard
    is kept, because the day that repository issues a bare `UPDATE ... SET` the property
    stops being free.

    What this case does pin is the other half, which is not free: a real change still
    writes, and it writes the value the lane asked for. Two separate transactions
    throughout, because `now()` is `transaction_timestamp()` and is frozen for the life
    of one.
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
