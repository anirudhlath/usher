"""The four watch actions through a real request, a real schema, a real upsert.

**What only this level can see.** `tests/unit/test_api_watch.py` drives the
same four routes over port fakes, so what is left here is everything the fakes
stand in for:

- the **un-overridden** dependency graph -- `get_watch_write_service`,
  `get_session`, `get_default_user_id` and four repositories resolving through
  FastAPI's own machinery against Postgres, which is a startup error a direct
  call cannot produce;
- `set_from_client`'s real statement: `origin = 'api'`, `played =
  excluded.played`, `play_count = GREATEST(watch_states.play_count, 1)` and the
  `last_played_at` CASE. `FakeWatchStateRepository` spells all four in Python
  and cannot disagree with itself;
- `trg_watch_states_set_updated_at`, the `BEFORE UPDATE` trigger that owns
  `updated_at` and is the entire mechanism behind "a client write wins over a
  walk in flight" -- the fake stores a Python `now()` there instead;
- the **foreign key**. `watch_states.title_id` references `titles(id)` with
  `ON DELETE RESTRICT`, so a write for an id that names no row is an
  `IntegrityError` rather than a phantom dict entry: the route's existence read
  is the difference between a 404 and a 500 carrying a constraint name, and
  only this file can tell;
- `PostgresMediaItemRepository.list_for_title`'s `AND episode_id IS NULL`
  against a real series with real episode rows;
- and the commit itself, read **from a second connection at the instant of the
  publish** -- ADR-0033's own measurement shape. A fake commit is a counter;
  this is the only place "an event is a statement about committed state" is a
  claim about the database rather than about a journal.

**One override and one only**: the event publisher, replaced by a probe that
reads `watch_states` on its own session while the frame is being published.
The probe is asserted non-empty before any claim is read out of it -- a probe
that never ran records nothing, and every absence claim over it passes.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request, and `watch_states`' two target foreign keys are
`RESTRICT` rather than `CASCADE` -- deliberately, so nothing silently destroys
watch history -- which means the rows this file writes have to go before the
titles they point at.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.api.deps import get_event_publisher
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import MediaItemUpsert

SECRET_KEY = "0123456789abcdef0123456789abcdef"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
# Every title this file writes carries it, so teardown deletes exactly what
# this file created rather than emptying a table another committing file uses.
MARK = "Watch Route Case"
MOVIE_EXTERNAL_ID = "movie-watch-0"
SERIES_EXTERNAL_ID = "series-watch-0"
EPISODE_COUNT = 3


class _CommitProbe(EventPublisher):
    """Records each frame *and* what a second connection can see at that moment.

    The G1 harness shape, one route over: a publisher is only allowed to offer
    an event about committed state, and the only witness that can tell a
    committed row from an uncommitted one is a connection that is not the
    writer's.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], user_id_key: str) -> None:
        self._sessions = sessions
        self._key = user_id_key
        self.seen: list[tuple[ClientEventKind, int | None, bool | None]] = []
        self.published: list[ClientEvent] = []

    async def publish(self, event: ClientEvent) -> None:
        self.published.append(event)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT position_seconds, played FROM watch_states "
                        "WHERE title_id = CAST(:title_id AS uuid)"
                    ),
                    {"title_id": self._key},
                )
            ).one_or_none()
        self.seen.append(
            (
                event.kind,
                None if row is None else row.position_seconds,
                None if row is None else row.played,
            )
        )


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # `dependency_overrides` do not reach the lifespan, so a worker lane
        # here would claim the write-back jobs this file asserts on -- and
        # `watch_writeback` has no handler yet, so it would park them.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route reads and writes through its own session in its own transaction,
    so a test that seeded through a single shared transaction would be handing
    the app rows it cannot see -- and, here, would make the commit probe's
    answer meaningless.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


class _Seeded:
    def __init__(self) -> None:
        self.source_id = uuid.uuid4()
        self.movie_id = uuid.uuid4()
        self.series_id = uuid.uuid4()
        self.episode_ids = [uuid.uuid4() for _ in range(EPISODE_COUNT)]


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `watch_states` first: both its target foreign keys are RESTRICT, so
        # a title this file wrote cannot be deleted while a row points at it.
        await session.execute(
            text(
                "DELETE FROM watch_states WHERE title_id IN "
                "(SELECT id FROM titles WHERE name LIKE :mark) "
                "OR episode_id IN (SELECT id FROM episodes WHERE title_id IN "
                "(SELECT id FROM titles WHERE name LIKE :mark))"
            ),
            {"mark": f"{MARK}%"},
        )
        await session.execute(text("DELETE FROM jobs WHERE kind = 'watch_writeback'"))
        # Takes `media_items` and `source_credentials` with it.
        await session.execute(text("TRUNCATE sources CASCADE"))
        await session.execute(
            text("DELETE FROM titles WHERE name LIKE :mark"), {"mark": f"{MARK}%"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Seeded]:
    """A movie with one copy, and a series with one copy plus three episode
    copies -- which is what makes `AND episode_id IS NULL` observable at all.

    Three rather than twenty thousand: the clause is the thing under test and
    it does not care how many rows it excludes.
    `tests/unit/test_services_watch_write.py` carries the twenty-episode case
    that names the measured read.
    """
    await _wipe(sessions)
    fixture = _Seeded()
    async with sessions() as session:
        await PostgresSourceRepository(session).add(
            Source(
                id=fixture.source_id,
                kind=SourceKind.EMBY,
                name="Living Room Emby",
                base_url="https://emby.invalid",
                credentials_ref="ref-watch-route",
                device_id=str(uuid.uuid4()),
            )
        )
        titles = PostgresTitleRepository(session)
        await titles.add(
            Title(
                id=fixture.movie_id,
                kind=TitleKind.MOVIE,
                name=f"{MARK} Movie",
                sort_name=f"{MARK} Movie",
            )
        )
        await titles.add(
            Title(
                id=fixture.series_id,
                kind=TitleKind.SERIES,
                name=f"{MARK} Series",
                sort_name=f"{MARK} Series",
            )
        )
        season = Season(title_id=fixture.series_id, season_number=1)
        episodes = PostgresEpisodeRepository(session)
        await episodes.upsert_seasons([season])
        await episodes.upsert_episodes(
            [
                Episode(
                    id=episode_id,
                    title_id=fixture.series_id,
                    season_id=season.id,
                    season_number=1,
                    episode_number=number + 1,
                )
                for number, episode_id in enumerate(fixture.episode_ids)
            ]
        )
        await PostgresMediaItemRepository(session).upsert_many(
            [
                _copy(fixture.source_id, MOVIE_EXTERNAL_ID, title_id=fixture.movie_id),
                _copy(fixture.source_id, SERIES_EXTERNAL_ID, title_id=fixture.series_id),
                *[
                    _copy(
                        fixture.source_id,
                        f"episode-watch-{number}",
                        title_id=fixture.series_id,
                        episode_id=episode_id,
                    )
                    for number, episode_id in enumerate(fixture.episode_ids)
                ],
            ]
        )
        await session.commit()
    try:
        yield fixture
    finally:
        await _wipe(sessions)


def _copy(
    source_id: uuid.UUID,
    external_id: str,
    *,
    title_id: uuid.UUID,
    episode_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        hdr_format=None,
        audio_channels=2,
        file_size_bytes=1,
        runtime_seconds=5400,
        added_at=None,
        last_seen_at=SEEN_AT,
    )


@pytest_asyncio.fixture
async def probe(sessions: async_sessionmaker[AsyncSession], seeded: _Seeded) -> _CommitProbe:
    return _CommitProbe(sessions, str(seeded.movie_id))


@pytest.fixture
def app(settings: Settings, probe: _CommitProbe) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_event_publisher] = lambda: probe
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


async def _watch_state(
    sessions: async_sessionmaker[AsyncSession], *, title_id: uuid.UUID
) -> Row[tuple[int, bool, int, object, str, object]]:
    async with sessions() as session:
        return (
            await session.execute(
                text(
                    "SELECT position_seconds, played, play_count, last_played_at, "
                    "origin, updated_at FROM watch_states "
                    "WHERE title_id = CAST(:title_id AS uuid)"
                ),
                {"title_id": str(title_id)},
            )
        ).one()


async def _write_back_keys(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessions() as session:
        rows = await session.execute(
            text("SELECT key, priority FROM jobs WHERE kind = 'watch_writeback' ORDER BY key")
        )
        return [row.key for row in rows]


async def test_a_put_writes_a_row_the_next_sync_will_not_overwrite(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`origin = 'api'` is the correctness property, read off the real column.

    It is what stops the next walk mistaking Usher's own write for the
    source's truth and round-tripping it back -- and the `updated_at` beside it
    is the trigger's, which is what makes this row newer than any walk that
    started before the request.
    """
    response = await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 1840, "played": False}
    )

    assert response.status_code == 200, response.text
    row = await _watch_state(sessions, title_id=seeded.movie_id)
    assert row.position_seconds == 1840
    assert row.played is False
    assert row.origin == "api"


async def test_the_row_is_committed_by_the_time_the_frame_is_published(
    client: AsyncClient, seeded: _Seeded, probe: _CommitProbe
) -> None:
    """ADR-0033, measured the way ADR-0033 was measured.

    A second connection reads `watch_states` from inside `publish`. Postgres
    never shows another transaction's uncommitted row version, so "the row is
    there" is exactly "the write committed first" -- and against a service
    that published before committing, every entry reads `None`.

    The probe's own recording is asserted non-empty first: a probe that never
    ran records nothing, and every absence claim over it passes.
    """
    await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 1840, "played": False}
    )

    assert probe.seen, "the probe never ran, so nothing below is evidence"
    assert [kind for kind, _, _ in probe.seen] == [
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.ROW_INVALIDATED,
        ClientEventKind.WATCHSTATE_UPDATED,
    ]
    assert all(position == 1840 for _, position, _ in probe.seen), probe.seen
    assert all(played is False for _, _, played in probe.seen), probe.seen


async def test_a_series_write_enqueues_one_job_and_not_one_per_episode(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`list_for_title`'s `AND episode_id IS NULL` against real rows.

    The premise is the fixture: the series carries one copy of its own and
    three episode copies that all name the same `title_id`, asserted here
    because a seed that wrote no episode rows would satisfy the answer below
    while measuring nothing.
    """
    async with sessions() as session:
        rows = await session.execute(
            text(
                "SELECT count(*) AS n FROM media_items "
                "WHERE title_id = CAST(:title_id AS uuid) AND episode_id IS NOT NULL"
            ),
            {"title_id": str(seeded.series_id)},
        )
        assert rows.one().n == EPISODE_COUNT

    response = await client.put(
        f"/watch/titles/{seeded.series_id}", json={"position_seconds": 61, "played": False}
    )

    assert response.status_code == 200, response.text
    assert await _write_back_keys(sessions) == [SERIES_EXTERNAL_ID]


async def test_an_episode_write_enqueues_the_episodes_own_file(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`list_for_episode` is the other statement, and it answers with exactly
    the row `list_for_title` excludes."""
    response = await client.put(
        f"/watch/episodes/{seeded.episode_ids[1]}",
        json={"position_seconds": 61, "played": False},
    )

    assert response.status_code == 200, response.text
    assert await _write_back_keys(sessions) == ["episode-watch-1"]


async def test_the_write_back_job_is_committed_by_the_request(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The enqueue rides `get_session`'s commit rather than the service's, so
    the row is only durable once the handler has returned.

    Read on a second connection, which is the only reader that can tell a
    committed job from one the request is still holding.
    """
    await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 61, "played": False}
    )

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT key, priority, status FROM jobs "
                    "WHERE kind = 'watch_writeback' AND key = :key"
                ),
                {"key": MOVIE_EXTERNAL_ID},
            )
        ).one()
    assert row.priority == 80
    assert row.status == "pending"


async def test_marking_played_twice_does_not_advance_the_count_twice(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`GREATEST(watch_states.play_count, 1)`, which is Emby's own
    `POST /PlayedItems` semantics measured against 4.9.5.0: it advances to 1
    idempotently rather than incrementing. A local `play_count + 1` would
    diverge from the source on the second press, and the write-back would then
    carry a number Usher invented.
    """
    await client.post(f"/watch/titles/{seeded.movie_id}/played")
    first = await _watch_state(sessions, title_id=seeded.movie_id)

    await client.post(f"/watch/titles/{seeded.movie_id}/played")

    second = await _watch_state(sessions, title_id=seeded.movie_id)
    assert first.play_count == 1
    assert second.play_count == 1


async def test_unmarking_played_keeps_the_position_the_count_and_the_date(
    client: AsyncClient, seeded: _Seeded, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """M3's destructive-route finding, against the real `CASE` clauses.

    Emby's `DELETE /Users/{u}/PlayedItems/{item}` resets `PlayCount`, clears
    `LastPlayedDate` **and** clears a non-zero resume position. All three
    survive here, and all three are separate `SET` expressions that could each
    have been written the other way.
    """
    await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 4000, "played": True}
    )
    played = await _watch_state(sessions, title_id=seeded.movie_id)
    assert played.play_count == 1 and played.last_played_at is not None

    response = await client.request("DELETE", f"/watch/titles/{seeded.movie_id}/played")

    assert response.status_code == 200, response.text
    row = await _watch_state(sessions, title_id=seeded.movie_id)
    assert row.played is False
    assert row.position_seconds == 4000
    assert row.play_count == 1
    assert row.last_played_at == played.last_played_at


async def test_an_unknown_title_is_a_404_rather_than_a_foreign_key_violation(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The reason the route reads `titles` before it writes.

    `watch_states.title_id` references `titles(id)`, so without that read this
    request is an `IntegrityError` -- a 500 carrying a constraint name for what
    is plainly a client error, and one that poisons the request's session on
    the way out. Only this file can tell the two apart: against the fake the
    unchecked write simply succeeds.
    """
    unknown = uuid.uuid4()

    response = await client.put(
        f"/watch/titles/{unknown}", json={"position_seconds": 61, "played": False}
    )

    assert response.status_code == 404, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["code"] == ProblemCode.NOT_FOUND.value
    assert await _write_back_keys(sessions) == []


async def test_a_repeat_write_of_identical_state_publishes_nothing(
    client: AsyncClient, seeded: _Seeded, probe: _CommitProbe
) -> None:
    """The changed-row guard against the real statement, where `updated_at`
    really is trigger-owned and really does move on every write -- which is
    the thing that makes a guard spelled `before != after` dead.
    """
    await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 61, "played": False}
    )
    assert probe.published, "the premise: the first write did publish"
    probe.published.clear()

    await client.put(
        f"/watch/titles/{seeded.movie_id}", json={"position_seconds": 61, "played": False}
    )

    assert probe.published == []
