"""PRD 06's "served stale while refreshing", against a real Postgres."""

import asyncio
import time
import uuid
import weakref
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
async def household(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The singleton default user's id: the cache key, and what the queue hands the lane.

    Created here rather than read, because the route's own `get_default_user` would
    create it on the first request and a case that planted a screen before that would
    key it to a household that does not exist yet.
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
                # `GET /home` promotes every skeleton it draws, and
                # `get_session` commits at the end of a successful request, so
                # each `/home` below leaves an `enrich` row per planted title.
                # **Before the title** -- the job's `key` is the title's id as
                # text, so once the title row is gone nothing identifies it.
                await session.execute(
                    text(
                        "DELETE FROM jobs WHERE kind = 'enrich' AND key IN "
                        "(SELECT id::text FROM titles WHERE id = :id)"
                    ),
                    {"id": title_id},
                )
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
    """Every ORM session's transaction boundaries, attributed to the task that opened it."""

    boundaries: list[_Boundary] = field(default_factory=list)
    commits: set[int] = field(default_factory=set)
    owners: dict[int, str] = field(default_factory=dict)
    held: list[Session] = field(default_factory=list)
    pinned: set[int] = field(default_factory=set)

    def pin(self, session: Session) -> int:
        """Take a strong reference to `session`, then return its address.

        The order is the whole point: once the reference is held the address
        cannot be handed to another object, so the integer this returns keys
        one session for the log's lifetime. Checking `pinned` rather than
        scanning `held` is sound *because* of that -- an address already in the
        set belongs to an object this log is still holding, so it cannot be a
        different session wearing a recycled address.
        """
        identity = id(session)
        if identity not in self.pinned:
            self.pinned.add(identity)
            self.held.append(session)
        return identity

    def record(self, kind: str, session: Session) -> None:
        identity = self.pin(session)
        if identity not in self.owners:
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

    def record_commit(self, session: Session) -> None:
        """Credit a commit to the session that made it, pinning it first.

        `after_commit` fires *before* `after_transaction_end`, so at this moment
        nothing else has pinned the session yet. Writing the address down without
        holding it would leave `commits` carrying an address the log does not own, and
        a later session landing on it inherits the credit -- a false green on
        `request_sessions <= commits`, never a red.
        """
        self.commits.add(self.pin(session))

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
        log.record_commit(session)

    event.listen(Session, "after_begin", began)
    event.listen(Session, "after_transaction_end", ended)
    event.listen(Session, "after_commit", committed)
    try:
        yield log
    finally:
        event.remove(Session, "after_begin", began)
        event.remove(Session, "after_transaction_end", ended)
        event.remove(Session, "after_commit", committed)

    # **The premise every identity comparison in this file rests on, asserted rather
    # than assumed.** `held` is a deliberate leak and reads like one, so the failure
    # mode worth guarding is somebody tidying it away: a log that pins nothing produces
    # exactly the same green as a log that pins everything, right up until two sessions
    # share an address.
    addresses = [id(one) for one in log.held]
    assert len(set(addresses)) == len(addresses), (
        "two sessions this log holds share an address, which cannot happen "
        "while both are alive -- so `held` is not holding what it recorded"
    )
    keyed_on = set(log.owners) | log.commits
    assert keyed_on <= set(addresses), (
        "the log keys on addresses it does not hold, so they can be recycled "
        f"under it: {sorted(keyed_on - set(addresses))} recorded, "
        f"{len(addresses)} sessions held"
    )


# The denominator for the recycling control below, named once so the number in
# `_SessionLog`'s docstring and the number this file exercises are the same.
_RECYCLE_TRIALS = 2000


async def test_the_session_log_holds_every_session_it_records_so_no_address_is_recycled() -> None:
    """`_SessionLog.held` is the whole of why `id(session)` is a safe key.

    `held` is a deliberate leak and reads like an oversight, so a `held` deleted as one
    looks exactly like a `held` that works. Every other assertion in this file would
    then be comparing recycled addresses, and
    `refresh_sessions.isdisjoint(request_sessions)` would accuse the code of the one
    defect serve-stale exists to prevent.

    Two positive controls first, because without them an interpreter that never freed a
    `Session` and never reused an address would pass this case for the wrong reason.
    """
    # Control 1: a `Session` nothing holds is refcount-freed by `del`, with no
    # collector pass. If it were not, `held` would be pinning nothing and the
    # guard below would pass on an object that was never at risk.
    loose = Session()
    loose_ref = weakref.ref(loose)
    del loose
    assert loose_ref() is None, (
        "a bare Session outlived `del` on this interpreter, so this case cannot "
        "tell a pinned session from an unpinned one"
    )

    # Control 2: the addresses really are handed out again. Asserted as an
    # inequality rather than against a count, because how many distinct `id()`
    # values come back is an allocator detail and the claim is only that reuse
    # happens at all.
    addresses = set()
    for _ in range(_RECYCLE_TRIALS):
        churn = Session()
        addresses.add(id(churn))
        churn.close()
        del churn
    assert len(addresses) < _RECYCLE_TRIALS, (
        f"{_RECYCLE_TRIALS} created-and-freed Sessions occupied {len(addresses)} "
        "distinct addresses -- this allocator recycles nothing, so `held` is "
        "guarding against a hazard that does not exist here and this case proves "
        "nothing"
    )

    # The guard: the log, and only the log, keeps the recorded session alive.
    log = _SessionLog()
    recorded = Session()
    recorded_ref = weakref.ref(recorded)
    recorded_id = id(recorded)
    log.record("begin", recorded)
    del recorded
    survivor = recorded_ref()
    assert survivor is not None, (
        "_SessionLog.record kept no reference to the session it recorded, so "
        f"address {recorded_id} is free for the next Session to land on and "
        "every identity comparison in this file is comparing addresses rather "
        "than sessions"
    )
    assert id(survivor) == recorded_id, "the pinned session moved, which cannot happen"
    assert recorded_id in log.owners, "the log recorded no owner for the session it held"


async def test_the_session_log_holds_the_session_at_the_moment_it_credits_a_commit(
    session_log: _SessionLog,
) -> None:
    """The commit credit must pin for itself.

    It must not inherit a pin from a handler that happens to run next.
    """
    held_when_credited: list[bool] = []

    def observe(session: Session) -> None:
        held_when_credited.append(any(one is session for one in session_log.held))

    # Registered after the fixture's own `after_commit` handler, so it observes
    # the log in exactly the state that handler left it.
    event.listen(Session, "after_commit", observe)
    try:
        ghost = Session()
        ghost.commit()
        ghost_id = id(ghost)
    finally:
        event.remove(Session, "after_commit", observe)

    assert held_when_credited == [True], (
        "the log credited a commit to an address it was not holding: `commits` "
        "is fed by a handler that pins nothing, so a later session landing on "
        f"address {ghost_id} inherits the credit and `request_sessions <= "
        "commits` passes for a request that never committed"
    )
    assert ghost_id in session_log.commits, "the commit was not observed at all"


def _plant(app: FastAPI, household: uuid.UUID, screen: tuple[BuiltRow, ...]) -> None:
    """Make `screen` this household's cached entry, already expired and still inside its grace.

    A negative TTL rather than a stepped clock: `create_app` builds its cache
    over `datetime.now(UTC)`, and a real wall clock cannot be advanced. The
    arithmetic -- that an entry expired by a second is `STALE` rather than
    `ABSENT` -- is pinned in `tests/unit/test_services_home_stale.py`, so this
    file's premise is checked rather than assumed.
    """
    app.state.row_cache.put_screen(household, screen, ttl=-timedelta(seconds=1))


def _expire(app: FastAPI, household: uuid.UUID) -> None:
    """Expire the cached screen **and** the rows on it, without deleting either.

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
    """The whole feature, end to end.

    The lane is held back across the request so both orderings are facts rather than
    races.
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
    """The refresh's session is genuinely new, shown by what it can see.

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
    """PRD 06's two layers, and the consequence of them a reader will not guess.

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
