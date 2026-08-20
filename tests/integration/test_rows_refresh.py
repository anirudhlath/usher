"""PRD 06's "served stale while refreshing", against a real Postgres.

**What only this level can see, and it is the whole reason M7 deferred the
feature rather than half-implementing it.** A background refresh needs a
session it did not get from a request: the request's own is committed and
closed by `get_session` when the handler returns, and sharing it with a task is
the `AsyncSession` concurrency hazard ADR-0025 refuses one layer up -- with the
same "usually works" signature, which is precisely why a refresh that shared
the request's session passes almost every test that does not look for it.
`tests/unit/test_api_lanes.py` can only count units of work opened against a
fake; here they are real sessions on a real pool.

**The lane is stopped for the first half of each case, deliberately.** That is
what turns two timing claims into two orderings a case can state without a
race: with nothing draining the queue, *"the response came back with the
refresh still queued"* is a fact rather than a coincidence, and *"the request's
session had already committed and closed before the refresh opened one"* is
guaranteed rather than usually true. Restarting the lane afterwards is the same
`LaneSupervisor` the lifespan built, over the same queue and the same cache.

**Stopping the lane makes the ordering true; it does not make it observable,
and issue #7 is the difference.** A held-back lane guarantees the refresh
happens after the request -- but the first version of the case then *read* that
ordering off two `time.monotonic()` windows over `id(session)`, which is a
guess about ownership dressed as a measurement. `_SessionLog` below records
which `asyncio` task opened each session and orders the boundaries by its own
counter, so claim 3 is checked against what was observed rather than against
when it happened.

**Both cases commit for real and clean up after themselves.** Their footprint
is the two titles they insert, deleted by id; the `users` row is a singleton
reached by `ON CONFLICT (name) DO NOTHING` and is left standing, as
`test_rows_route.py` leaves it.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert
from usher.services.rows.cache import Freshness

SECRET_KEY = "0123456789abcdef0123456789abcdef"

# The stale screen every case plants. A slug no provider mints, so "the route
# served the cached screen" is distinguishable from "the route composed a real
# one that happens to look similar" -- which an empty screen would not be.
PLANTED = BuiltRow(
    slug="planted-stale",
    title="Planted Stale",
    family=RowFamily.SOURCE,
    display_hint=DisplayHint.LANDSCAPE,
    ttl=timedelta(seconds=30),
    cards=(),
)


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # The push and worker lanes off; the `rows.refresh` lane is **not**
        # settings-gated and runs regardless, which is the subject here. Off
        # for the reason every other integration fixture turns them off:
        # `dependency_overrides` do not reach the lifespan, so a push lane
        # would build a real `EmbyAdapter` and open a socket.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route and the lane each commit from a session of their own, so reading
    or writing through the suite's shared transaction would be asking a
    connection that cannot see them -- and the whole point of the second case
    is a row committed on a *third*.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def household(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The singleton default user's id -- the cache key, and the value the
    queue hands to the lane.

    Created here rather than read, because the route's own `get_default_user`
    would create it on the first request and a case that planted a screen
    before that would key it to a household that does not exist yet.
    """
    async with sessions() as session:
        user_id = await ensure_default_user(session)
        await session.commit()
    return user_id


@pytest_asyncio.fixture
async def source(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Source]:
    """One source, so `media_items` has something to hang ownership off."""
    stored = Source(
        kind=SourceKind.EMBY,
        name=f"refresh-probe-{new_id()}",
        base_url="https://refresh.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with sessions() as session:
        await PostgresSourceRepository(session).add(stored)
        await session.commit()
    try:
        yield stored
    finally:
        async with sessions() as session:
            await session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": stored.id})
            await session.commit()


@pytest_asyncio.fixture
async def owned(
    sessions: async_sessionmaker[AsyncSession], source: Source
) -> AsyncIterator[Callable[[str], "asyncio.Future[uuid.UUID]"]]:
    """Commit one owned, freshly-added title and hand back its id.

    Freshly added so `RecentlyAddedProvider` -- the one provider that fires on
    a household that has watched nothing -- has something to build a row from.
    """
    planted: list[uuid.UUID] = []

    async def add(name: str) -> uuid.UUID:
        title = Title(
            id=new_id(), kind=TitleKind.MOVIE, name=name, sort_name=name.lower(), year=2026
        )
        now = datetime.now(UTC)
        async with sessions() as session:
            await PostgresTitleRepository(session).add(title)
            await PostgresMediaItemRepository(session).upsert_many(
                [
                    MediaItemUpsert(
                        source_id=source.id,
                        external_id=f"refresh-probe-{title.id}",
                        title_id=title.id,
                        episode_id=None,
                        container="mkv",
                        video_codec=None,
                        audio_codec=None,
                        width=None,
                        height=None,
                        hdr_format=None,
                        audio_channels=None,
                        file_size_bytes=None,
                        runtime_seconds=None,
                        added_at=now,
                        last_seen_at=now,
                    )
                ]
            )
            await session.commit()
        planted.append(title.id)
        return title.id

    try:
        yield add  # type: ignore[misc]
    finally:
        async with sessions() as session:
            for title_id in planted:
                await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title_id})
            await session.commit()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# The name `LaneSupervisor.start` gives the `rows.refresh` lane's task, and
# `_run_row_refresh` is the only thing that ever runs on it. Reading it back
# off `asyncio.current_task()` at the moment a session opens is therefore an
# **observation** of which side opened that session, which is what claim 3
# below needs and what a wall-clock window only approximates.
_REFRESH_TASK = "usher.lane.rows.refresh"

# The name this file gives the task it runs the request on, for the same
# reason. `ASGITransport` calls the app inline, so the route, its dependencies
# and `get_session`'s commit all run on whatever task awaited the response --
# so naming that task is enough to attribute the request's session to the
# request, with no clock in it.
_REQUEST_TASK = "the-request-under-test"


@dataclass(frozen=True, slots=True)
class _Boundary:
    """One observed transaction boundary.

    `seq` is the log's own counter and is what the ordering assertion reads:
    a total order over *recorded events*, which cannot be inverted by two
    boundaries landing in the same clock tick. `at` is carried for the failure
    message only -- CLAUDE.md's overlap rule wants the interval each side
    occupied stated, and a bare sequence number states nothing a reader can
    size.
    """

    seq: int
    kind: str
    session: int
    owner: str
    at: float


@dataclass(slots=True)
class _SessionLog:
    """Every ORM session's transaction boundaries, attributed to the task that
    opened it.

    Registered on `sqlalchemy.orm.Session` itself rather than on one factory,
    because the claim is about *two* factories: the app's, which the request
    and the lane both draw from, and this file's own.

    **Two things this deliberately does not do, and both were how the previous
    version was a race rather than an observation.**

    It does not classify a session by *when* it began. A wall-clock window
    says "some session started while the request was in flight", which is a
    statement about the clock; `asyncio.current_task().get_name()` at
    `after_begin` says which side opened it, which is the statement claim 3
    actually makes. Verified directly that the name is readable there:
    SQLAlchemy's greenlet bridge runs the sync event on the awaiting task, so
    a session opened under `usher.lane.rows.refresh` reports that name.

    And it does not let `id(session)` be recycled. **`id()` is a CPython
    address and CPython reuses addresses**: measured on this host on
    2026-08-19, eight sessions opened back to back through this same listener
    produced **five distinct `id()` values**. The request's `Session` is
    unreachable long before the refresh opens one -- measured in **40 of 40**
    request/refresh cycles -- so its address is free for the refresh's session
    to land on, and `refresh_sessions.isdisjoint(request_sessions)` would then
    report *"the refresh reused the request's session"* about two different
    objects. `held` keeps every observed session alive for the length of the
    case, which makes the identity unique by construction instead of by luck.
    """

    boundaries: list[_Boundary] = field(default_factory=list)
    commits: set[int] = field(default_factory=set)
    owners: dict[int, str] = field(default_factory=dict)
    held: list[Session] = field(default_factory=list)

    def record(self, kind: str, session: Session) -> None:
        identity = id(session)
        if identity not in self.owners:
            self.held.append(session)
            task = asyncio.current_task()
            self.owners[identity] = task.get_name() if task is not None else "<no task>"
        self.boundaries.append(
            _Boundary(
                seq=len(self.boundaries),
                kind=kind,
                session=identity,
                owner=self.owners[identity],
                at=time.monotonic(),
            )
        )

    def opened_by(self, owner: str) -> set[int]:
        return {identity for identity, name in self.owners.items() if name == owner}

    def last_end(self, sessions: set[int]) -> _Boundary:
        return max(
            (one for one in self.boundaries if one.kind == "end" and one.session in sessions),
            key=lambda one: one.seq,
        )

    def first_begin(self, sessions: set[int]) -> _Boundary:
        return min(
            (one for one in self.boundaries if one.kind == "begin" and one.session in sessions),
            key=lambda one: one.seq,
        )


@pytest.fixture
def session_log() -> Iterator[_SessionLog]:
    log = _SessionLog()

    def began(session: Session, transaction: object, connection: object) -> None:
        log.record("begin", session)

    def ended(session: Session, transaction: object) -> None:
        log.record("end", session)

    def committed(session: Session) -> None:
        log.commits.add(id(session))

    event.listen(Session, "after_begin", began)
    event.listen(Session, "after_transaction_end", ended)
    event.listen(Session, "after_commit", committed)
    try:
        yield log
    finally:
        event.remove(Session, "after_begin", began)
        event.remove(Session, "after_transaction_end", ended)
        event.remove(Session, "after_commit", committed)


def _plant(app: FastAPI, household: uuid.UUID, screen: tuple[BuiltRow, ...]) -> None:
    """Make `screen` this household's cached entry, already expired and still
    inside its grace.

    A negative TTL rather than a stepped clock: `create_app` builds its cache
    over `datetime.now(UTC)`, and a real wall clock cannot be advanced. The
    arithmetic -- that an entry expired by a second is `STALE` rather than
    `ABSENT` -- is pinned in `tests/unit/test_services_home_stale.py`, so this
    file's premise is checked rather than assumed.
    """
    app.state.row_cache.put_screen(household, screen, ttl=-timedelta(seconds=1))


def _expire(app: FastAPI, household: uuid.UUID) -> None:
    """Expire the cached screen **and** the rows on it, without deleting
    either.

    `RowCache.invalidate` would drop both outright, which makes the next
    request a hard miss rather than a stale serve -- the opposite of what
    these cases are about. Re-putting each entry with a negative TTL leaves
    them present and expired, which is the state serve-stale is defined over.
    """
    cache = app.state.row_cache
    screen = cache.read_screen(household).screen or ()
    for row in screen:
        cache.put_row(household, row.slug, row, ttl=-timedelta(seconds=1))
    _plant(app, household, screen)


async def _drain(until: Callable[[], bool], *, bound: float = 20.0) -> None:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if until():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the rows.refresh lane never finished")


async def test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own(
    app: FastAPI,
    client: AsyncClient,
    household: uuid.UUID,
    session_log: _SessionLog,
    owned: Callable[[str], "asyncio.Future[uuid.UUID]"],
) -> None:
    """The whole feature, end to end, with the lane held back across the
    request so both orderings are facts rather than races.

    Three claims, and the third is the one M7 deferred the feature for:

    1. **The response is the stale screen.** A slug no provider mints, so this
       cannot be satisfied by a route that composed a fresh one.
    2. **The request did not wait for the refresh.** With the lane stopped,
       the key is still sitting in the queue when the response arrives -- the
       strongest available spelling of it at the HTTP boundary, and it fails
       against an implementation that awaited the refresh by *hanging*, which
       is why `tests/unit/test_services_home_stale.py` also drives the
       coroutine by hand.
    3. **The refresh opened a session of its own, after the request's had
       committed and closed.** Distinct `Session` identities, and the
       request's last transaction end strictly before the refresh's first
       begin. A refresh sharing the request's session satisfies claims 1 and
       2 exactly as well.

    **Claim 3 is read off a session log, not off the clock, and issue #7 is
    why.** Both halves of it used to be inferred from wall-clock windows over
    `id(session)`: *"a session began between these two `time.monotonic()`
    readings, therefore it is the request's"*. That is a race twice over --
    `id()` is a reusable address, and a window is not an owner -- and the
    repair is to observe both. Each session carries the name of the
    `asyncio` task that opened it, and the ordering is asserted over the log's
    own event counter rather than over two floats. See `_SessionLog`.
    """
    await owned("A Film That Arrived Before The Request")
    await app.state.lanes.stop()
    assert app.state.lanes.rows_refreshing() is False, "the lane must really be held back"
    _plant(app, household, (PLANTED,))

    # A task of its own, and named, because the name is the whole of how the
    # sessions below are attributed. `await client.get(...)` would run on
    # pytest-asyncio's task, whose name this file does not choose and which
    # every other `await` in the case shares.
    response = await asyncio.create_task(client.get("/home"), name=_REQUEST_TASK)

    assert response.status_code == 200
    assert [row["slug"] for row in response.json()["rows"]] == ["planted-stale"]
    assert app.state.row_refreshes.depth == 1, (
        "the response arrived with the refresh still queued: the request did not wait"
    )
    assert app.state.row_refreshes.pending == frozenset({household})

    request_sessions = session_log.opened_by(_REQUEST_TASK)
    assert request_sessions, "the request opened no session at all, so this proves nothing"
    assert request_sessions <= session_log.commits, "get_session is the commit boundary"

    await app.state.lanes.start()
    await _drain(lambda: app.state.row_refreshes.pending == frozenset())

    refresh_sessions = session_log.opened_by(_REFRESH_TASK)
    assert refresh_sessions, (
        f"no session was opened on {_REFRESH_TASK!r}, so either the refresh never ran "
        f"or it ran somewhere this case cannot see: observed owners "
        f"{sorted(set(session_log.owners.values()))}"
    )
    assert refresh_sessions.isdisjoint(request_sessions), (
        "the refresh reused the request's session -- the AsyncSession hazard "
        "that usually works, which is how it ships"
    )
    closed = session_log.last_end(request_sessions)
    opened = session_log.first_begin(refresh_sessions)
    assert closed.seq < opened.seq, (
        "the refresh's session began before the request's had ended, so the two "
        f"overlapped rather than followed: request closed {closed}, refresh opened {opened}"
    )

    read = app.state.row_cache.read_screen(household)
    assert read.freshness is Freshness.FRESH, "the stale entry was replaced"
    assert read.screen is not None
    assert [row.slug for row in read.screen] == ["recently-added"]


async def test_the_refresh_reads_state_committed_after_the_screen_was_cached(
    app: FastAPI,
    client: AsyncClient,
    household: uuid.UUID,
    owned: Callable[[str], "asyncio.Future[uuid.UUID]"],
) -> None:
    """**The refresh's session is genuinely new, shown by what it can see.**

    Identity is one half of "its own session"; freshness is the other, and it
    is the half a stale connection would fail. A title committed on a third
    session *after* the cached screen was built has to appear in the refreshed
    one -- which it cannot if the refresh read through a snapshot the request
    left behind, and which is only a decidable question because the lane is
    held back until the write has committed.

    The second title makes the assertion an ordering rather than a count: two
    cards where the cached screen had one, newer first, because
    `RecentlyAddedProvider` orders by `added_at DESC`.

    **The row entry is expired alongside the screen, and the case below is why
    that is a fixture choice rather than a cheat.** PRD 06 caches at two
    layers, and a screen refresh does not disturb a row whose own TTL has not
    moved -- so with `recently-added`'s five minutes still running this case
    would be asking the refresh to re-read something the composer is
    deliberately not going to re-read.
    """
    first = await owned("A Film That Arrived First")
    await app.state.lanes.stop()

    warm = await client.get("/home")
    assert warm.status_code == 200
    assert [card["title_id"] for card in warm.json()["rows"][0]["cards"]] == [str(first)]
    assert app.state.row_refreshes.depth == 0, "a cold compose schedules no refresh"

    second = await owned("A Film That Arrived After The Screen Was Cached")
    _expire(app, household)

    stale = await client.get("/home")
    assert [card["title_id"] for card in stale.json()["rows"][0]["cards"]] == [str(first)], (
        "the stale screen is the one that was cached, and it predates the second title"
    )

    await app.state.lanes.start()
    await _drain(lambda: app.state.row_refreshes.pending == frozenset())

    read = app.state.row_cache.read_screen(household)
    assert read.screen is not None
    assert [card.title_id for card in read.screen[0].cards] == [second, first], (
        "the refresh did not see a row committed after the cached screen was built"
    )


async def test_a_screen_refresh_reuses_a_row_whose_own_ttl_has_not_moved(
    app: FastAPI,
    client: AsyncClient,
    household: uuid.UUID,
    owned: Callable[[str], "asyncio.Future[uuid.UUID]"],
) -> None:
    """**PRD 06's two layers, and the consequence of them a reader will not
    guess** -- found by writing the case above without it and watching the
    refreshed screen come back unchanged.

    The screen is ~30 s and `recently-added` is five minutes, so a screen
    refresh re-proposes, re-selects and re-orders while *reusing* every row
    whose own TTL is still running. The refreshed screen is therefore a fresh
    composition of possibly-older rows, and a household can see a five-minute
    old shelf on a screen that is seconds old.

    That is the design rather than a defect -- rebuilding every row on every
    30 s screen expiry is precisely the cost the second layer exists to
    avoid -- and it is exactly why `RowCache.get_row` has no grace window of
    its own: the refresh unit is a *screen*, one entry per household, and a
    per-row grace with no per-row refresh behind it would serve stale rows
    that nothing ever replaces.
    """
    first = await owned("A Film That Arrived First")
    await app.state.lanes.stop()
    await client.get("/home")

    await owned("A Film The Row Cache Will Hide For Five Minutes")
    # The screen only. The row's own five minutes are untouched, which is the
    # single difference from the case above.
    _plant(app, household, app.state.row_cache.read_screen(household).screen or ())
    await client.get("/home")

    await app.state.lanes.start()
    await _drain(lambda: app.state.row_refreshes.pending == frozenset())

    read = app.state.row_cache.read_screen(household)
    assert read.freshness is Freshness.FRESH, "the screen really was refreshed"
    assert read.screen is not None
    assert [card.title_id for card in read.screen[0].cards] == [first], (
        "a screen refresh must not rebuild a row whose own TTL is still running"
    )
