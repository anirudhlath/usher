"""`GET /titles/{id}/similar` through a real request against a real schema.

**What only this level can see.** `tests/unit/test_api_similar.py` drives the
route over fakes; `tests/integration/test_services_similar.py` drives
`SimilarityService` over real Postgres directly. What is left is the request
itself: that `api/deps.py`'s `get_similarity_service` wiring actually resolves
against a real session, that `count_stale`'s real SQL predicate (not the
fake's Python comparison -- `testing-discipline.md`'s staleness-gauge finding)
reaches the wire scoped to the right seed, and the risk B8's own plan names --
**the route only reads, so nothing commits** -- checked against real SQL
rather than argued in a docstring.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

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
from usher.db.repositories.search import PostgresTitleNeighborRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.repository import ScoredNeighbor
from usher.services.similar import blend_fingerprint

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every title this file writes carries it, so teardown removes exactly what
# this file created -- `test_titles_route.py`'s convention, for the same
# reason: a committing test that left rows behind took down four cases in
# three other files that each passed in isolation.
MARK = "Similar Route Case"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one --
    the app's own session commits per request, so seeding through a shared,
    rolled-back transaction would hand it rows it cannot see."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `title_neighbors` carries `ON DELETE CASCADE` to `titles` on both
        # `title_id` and `neighbor_id` (`tests/fakes/title_neighbor_
        # repository.py`'s own docstring), so deleting the titles is enough.
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
    """Every SQL statement issued from every engine in the process, captured
    off `before_cursor_execute` -- `test_titles_route.py`'s own helper,
    copied rather than imported so this file has no import of a sibling test
    module's fixtures and parametrized cases."""
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


async def _given_title(sessions: async_sessionmaker[AsyncSession], name: str) -> Title:
    title = Title(kind=TitleKind.MOVIE, name=name, sort_name=f"{MARK} {name}", year=2021)
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _given_neighbors(
    sessions: async_sessionmaker[AsyncSession],
    seed: Title,
    neighbor: Title,
    *,
    fingerprint: str,
) -> None:
    async with sessions() as session:
        await PostgresTitleNeighborRepository(session).replace(
            [seed.id],
            [ScoredNeighbor(title_id=seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
            blend_fingerprint=fingerprint,
        )
        await session.commit()


async def test_the_route_resolves_through_the_real_wiring_and_reports_staleness(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The end-to-end check `tests/unit/test_api_similar.py` cannot make:
    `api/deps.py`'s `get_similarity_service` actually resolves against a real
    session, and the real `count_stale` SQL predicate -- not the fake's
    Python comparison, which `testing-discipline.md` records as the thing an
    inverted `WHERE blend_fingerprint <> :fp` survived against for a whole
    milestone -- reaches the wire scoped to this seed. Two seeds, one stale
    and one fresh in the *same* real table, for the same reason that finding
    gives: with only one kind present an inversion of the predicate answers
    correctly by luck of direction.
    """
    stale_seed = await _given_title(sessions, "Stale Seed")
    fresh_seed = await _given_title(sessions, "Fresh Seed")
    neighbor = await _given_title(sessions, "A Neighbour")

    await _given_neighbors(sessions, stale_seed, neighbor, fingerprint="an-old-blend")
    await _given_neighbors(sessions, fresh_seed, neighbor, fingerprint=blend_fingerprint())

    stale_body = (await client.get(f"/titles/{stale_seed.id}/similar")).json()
    fresh_body = (await client.get(f"/titles/{fresh_seed.id}/similar")).json()

    assert stale_body["stale"] is True
    assert stale_body["computed_at"] is not None
    assert [row["id"] for row in stale_body["neighbors"]] == [str(neighbor.id)]

    assert fresh_body["stale"] is False
    assert [row["id"] for row in fresh_body["neighbors"]] == [str(neighbor.id)]


async def test_a_title_with_no_neighbours_is_200_and_an_unknown_title_is_404(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    lonely = await _given_title(sessions, "Nothing Like It")

    lonely_response = await client.get(f"/titles/{lonely.id}/similar")
    assert lonely_response.status_code == 200
    assert lonely_response.json()["neighbors"] == []

    unknown_id = uuid.uuid4()
    unknown_response = await client.get(f"/titles/{unknown_id}/similar")
    assert unknown_response.status_code == 404
    assert unknown_response.headers["content-type"] == "application/problem+json"
    assert unknown_response.json()["code"] == "not_found"


async def test_the_route_issues_no_write_statement(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    """B8's own risk, checked against real SQL rather than argued in a
    docstring: `SimilarityService`'s fourth constructor argument is
    `session.commit`, the same callable `get_session` calls at the end of
    every request -- and this route only reads. A write here would mean the
    wiring meant for `usher similar --rebuild` leaked onto a `GET`."""
    seed = await _given_title(sessions, "A Read Only Seed")
    neighbor = await _given_title(sessions, "Its Neighbour")
    await _given_neighbors(sessions, seed, neighbor, fingerprint=blend_fingerprint())

    statement_counter.clear()
    response = await client.get(f"/titles/{seed.id}/similar")
    assert response.status_code == 200

    upper = [statement.strip().upper() for statement in statement_counter]
    assert upper, "no statement was captured, so this proves nothing"
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in upper), (
        statement_counter
    )
