"""The series hierarchy through real requests against a real schema.

**What only this level can see.** `tests/unit/test_api_series.py` drives the
three routes over `FakeEpisodeRepository`, whose ordering is Python's `sorted`
and whose keyset is a tuple comparison -- so the statement this milestone
actually ships, its `ORDER BY`, and its two-arm `WHERE`, are never executed
there. Here they are, against `pgvector/pgvector:pg17`, and the cost of a page
is counted off the statements the repositories really issued rather than off a
fake's call counter.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request even when the handler only read, and CLAUDE.md records
what leaving `titles` behind did to four tests in three other files, each of
which passed in isolation. `seasons` and `episodes` cascade from `titles`, so
deleting this file's own titles is enough -- and the delete is bound to this
file's marker rather than emptying a table another committing file is using.
"""

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.title import Title

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every title this file writes carries it, so teardown can delete exactly what
# this file created rather than emptying a table another committing file is
# also using.
MARK = "Hierarchy Route Case"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a push lane here would build the real adapter against an unreachable
        # host and open a socket.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The app reads from its own session in its own transaction, so a fixture
    that seeded through the shared rolled-back one would be handing the route
    rows it cannot see.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # Bound rather than interpolated, and `titles` only: `seasons` and
        # `episodes` are both `ON DELETE CASCADE` from it, and no other table
        # in this file's fixtures references them.
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, from every engine in the process
    -- including the app's own, which is the one under measurement.

    Captured off `before_cursor_execute` rather than transcribed: M4 replaced
    two tasks that asserted on a hand-copied lookalike of a query, because the
    copy drifts from the repository and then reads like coverage.
    """
    seen: list[str] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def _given_title(
    sessions: async_sessionmaker[AsyncSession], name: str, kind: TitleKind = TitleKind.SERIES
) -> Title:
    title = Title(kind=kind, name=f"{MARK} {name}", sort_name=f"{MARK} {name}")
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _given_seasons(
    sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID, numbers: Sequence[int]
) -> dict[int, uuid.UUID]:
    """Seeded in the order given, so a caller can make the minted UUIDv7s
    disagree with the season numbers on purpose."""
    async with sessions() as session:
        repository = PostgresEpisodeRepository(session)
        await repository.upsert_seasons(
            [Season(title_id=title_id, season_number=number) for number in numbers]
        )
        resolved = await repository.resolve_seasons([(title_id, number) for number in numbers])
        await session.commit()
    return {number: resolved[(title_id, number)] for number in numbers}


async def _given_episodes(
    sessions: async_sessionmaker[AsyncSession],
    title_id: uuid.UUID,
    season_id: uuid.UUID,
    season_number: int,
    numbers: Sequence[int],
) -> None:
    async with sessions() as session:
        await PostgresEpisodeRepository(session).upsert_episodes(
            [
                Episode(
                    title_id=title_id,
                    season_id=season_id,
                    season_number=season_number,
                    episode_number=number,
                    name=f"Episode {number}",
                )
                for number in numbers
            ]
        )
        await session.commit()


async def test_a_series_answers_its_seasons_ordered_by_postgres(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The `ORDER BY season_number` is the real statement's, not a `sorted`
    call in a fake.

    Seeded in descending order so the minted UUIDv7s descend with the season
    numbers, and the premise says so: without that, `ORDER BY id` returns the
    same list and the ordering is untested. Postgres would also be free to
    return the rows in physical (insertion) order without the clause, which
    here is exactly the reverse of the right answer.
    """
    series = await _given_title(sessions, "Ordered Series")
    seasons = await _given_seasons(sessions, series.id, [2, 1, 0])
    assert seasons[0] > seasons[2], (
        "the premise: ids were minted in descending season order, so `ORDER BY id` and "
        "`ORDER BY season_number` disagree"
    )

    response = await client.get(f"/series/{series.id}/seasons")

    assert response.status_code == 200
    body = response.json()
    assert [one["season_number"] for one in body["seasons"]] == [0, 1, 2]
    assert body["seasons"][0]["id"] == str(seasons[0])


async def test_a_season_pages_through_postgres_and_the_pages_abut(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The keyset `WHERE` this milestone ships, executed.

    Nine episodes at `limit=4` -- deliberately not a divisor of nine, so the
    walk ends on a short page -- and then a second walk at `limit=3`, which
    exhausts the season exactly and must still carry no cursor. That second
    case is the one ADR-0034 says is invisible outside `count % limit == 0`.
    """
    series = await _given_title(sessions, "Paged Series")
    seasons = await _given_seasons(sessions, series.id, [1])
    await _given_episodes(sessions, series.id, seasons[1], 1, list(range(1, 10)))

    for limit in (4, 3):
        walked: list[int] = []
        cursor: str | None = None
        for _ in range(5):
            query = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
            page = await client.get(f"/seasons/{seasons[1]}/episodes{query}")
            assert page.status_code == 200
            body = page.json()
            walked.extend(one["episode_number"] for one in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert walked == list(range(1, 10)), f"limit={limit} duplicated or dropped an episode"
        assert cursor is None, f"limit={limit} did not finish"


async def test_a_seasons_page_carries_no_other_seasons_episodes(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The season scope, against a table holding both seasons' rows.

    Every series has an S01E01, so the distractor carries the same numbers --
    a read that lost `season_id` answers with six rows here and would satisfy
    any membership assertion.
    """
    series = await _given_title(sessions, "Scoped Series")
    seasons = await _given_seasons(sessions, series.id, [1, 2])
    await _given_episodes(sessions, series.id, seasons[1], 1, [1, 2, 3])
    await _given_episodes(sessions, series.id, seasons[2], 2, [1, 2, 3])

    page = await client.get(f"/seasons/{seasons[2]}/episodes")

    assert page.status_code == 200
    items = page.json()["items"]
    assert [one["episode_number"] for one in items] == [1, 2, 3]
    assert {one["season_number"] for one in items} == {2}
    assert {one["season_id"] for one in items} == {str(seasons[2])}


async def test_an_episode_reads_back_with_the_ids_a_client_climbs_with(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    series = await _given_title(sessions, "Detail Series")
    seasons = await _given_seasons(sessions, series.id, [1])
    await _given_episodes(sessions, series.id, seasons[1], 1, [1])
    listed = (await client.get(f"/seasons/{seasons[1]}/episodes")).json()["items"]
    assert len(listed) == 1, "the premise: the episode exists to be fetched by id"

    response = await client.get(f"/episodes/{listed[0]['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["title_id"] == str(series.id)
    assert body["season_id"] == str(seasons[1])
    assert (body["season_number"], body["episode_number"]) == (1, 1)


async def test_a_movie_answers_200_and_an_id_no_title_carries_answers_404(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The distinguishability case, against a real `titles` table -- the fake
    arm cannot tell a missing row from a title with no seasons any better than
    this one, but only here is the existence read a real statement."""
    movie = await _given_title(sessions, "A Film", kind=TitleKind.MOVIE)

    empty = await client.get(f"/series/{movie.id}/seasons")
    assert empty.status_code == 200
    assert empty.json()["seasons"] == []

    missing = await client.get(f"/series/{new_id()}/seasons")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_the_seasons_route_costs_the_same_statements_however_many_seasons(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """Two statements, fixed: the title's existence, and the season list.

    What varies is the number of seasons and what is held fixed is the request
    -- which is the shape a statement-count assertion needs. A read per season
    is invisible when both runs return the same number of rows, and the
    obvious wrong implementation here is `list_for_title`, which answers the
    same question by reading the entire tree.
    """
    small = await _given_title(sessions, "Two Seasons")
    await _given_seasons(sessions, small.id, [1, 2])
    large = await _given_title(sessions, "Twenty-Five Seasons")
    seasons = await _given_seasons(sessions, large.id, list(range(1, 26)))
    for number, season_id in seasons.items():
        await _given_episodes(sessions, large.id, season_id, number, [1, 2, 3])

    statement_counter.clear()
    assert (await client.get(f"/series/{small.id}/seasons")).status_code == 200
    two = len(statement_counter)

    statement_counter.clear()
    listed = await client.get(f"/series/{large.id}/seasons")
    twenty_five = len(statement_counter)

    assert len(listed.json()["seasons"]) == 25, "the premise: the larger series really is larger"
    assert two == twenty_five == 2, (
        f"{two} statements for 2 seasons, {twenty_five} for 25 -- {statement_counter}"
    )


async def test_the_episodes_route_costs_one_statement_for_the_page_however_big(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """Two statements per page, fixed: the season's existence, and the page.

    The page size varies and the season is held fixed. This is the N+1 that
    `resolve_episodes` and `next_up` both exist to prevent, arriving at a
    route -- and 999,827 of the one measured source's 1,126,674 items are
    episodes, so a per-row read here is the defect batching exists to remove
    wearing a paged response.
    """
    series = await _given_title(sessions, "Counted Series")
    seasons = await _given_seasons(sessions, series.id, [1])
    await _given_episodes(sessions, series.id, seasons[1], 1, list(range(1, 61)))

    statement_counter.clear()
    small = await client.get(f"/seasons/{seasons[1]}/episodes?limit=2")
    small_statements = len(statement_counter)

    statement_counter.clear()
    large = await client.get(f"/seasons/{seasons[1]}/episodes?limit=50")
    large_statements = len(statement_counter)

    assert len(small.json()["items"]) == 2
    assert len(large.json()["items"]) == 50, "the premise: the larger page really is larger"
    assert small_statements == large_statements == 2, (
        f"{small_statements} statements for 2 episodes, {large_statements} for 50 -- "
        f"{statement_counter}"
    )


async def test_the_episode_route_costs_one_statement(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """`list_by_ids([id])` in one round trip, and never `list_for_title`,
    which would read 20,000 rows to find one."""
    series = await _given_title(sessions, "Single Episode Series")
    seasons = await _given_seasons(sessions, series.id, [1])
    await _given_episodes(sessions, series.id, seasons[1], 1, list(range(1, 21)))
    listed = (await client.get(f"/seasons/{seasons[1]}/episodes?limit=1")).json()["items"]

    statement_counter.clear()
    response = await client.get(f"/episodes/{listed[0]['id']}")

    assert response.status_code == 200
    assert len(statement_counter) == 1, statement_counter
