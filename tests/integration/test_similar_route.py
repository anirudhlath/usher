"""`GET /titles/{id}/similar` through a real request against a real schema."""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.llm_client import FakeLLMClient, usage
from tests.fakes.metadata_provider import FakeMetadataProvider
from usher.api.app import create_app
from usher.composition import build_pipeline, build_seed_row_service
from usher.config import Settings
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.search import PostgresTitleNeighborRepository
from usher.db.repositories.seed_row import PostgresSeedRowRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, ImageKind, TitleKind
from usher.domain.image import Image
from usher.domain.jobs import Job, JobKind
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.domain.title import Title
from usher.ports.repository import ScoredNeighbor
from usher.services.handlers import seed_row_handler
from usher.services.seed_row_recall import seed_row_recipe

SECRET_KEY = "0123456789abcdef0123456789abcdef"
# Every title this file writes carries it, so teardown removes exactly what
# this file created.
MARK = "Similar Route Case"
# The model the scripted client reports, so teardown removes its ledger rows.
LEDGER_MODEL = "similar-route-case/scripted"


def _settings(postgres_url: str, *, llm_enabled: bool) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
        llm_enabled=llm_enabled,
    )


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return _settings(postgres_url, llm_enabled=False)


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # The route commits its `seed_row` job for real, and `jobs` has no foreign key to
        # `titles`, so it goes first, keyed by the titles this file wrote.
        await session.execute(
            text(
                "DELETE FROM jobs WHERE kind = 'seed_row' AND key IN "
                "(SELECT id::text FROM titles WHERE sort_name LIKE :pattern)"
            ),
            {"pattern": f"{MARK} %"},
        )
        await session.execute(
            text("DELETE FROM llm_calls WHERE model = :model"), {"model": LEDGER_MODEL}
        )
        # `title_neighbors`, `seed_rows` and `images` cascade from `titles`.
        await session.execute(
            text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{MARK} %"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@asynccontextmanager
async def _serving(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    async with _serving(settings) as connected:
        yield connected


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement issued from every engine in the process.

    Captured off `before_cursor_execute` -- `test_titles_route.py`'s own helper, copied
    rather than imported so this file has no import of a sibling test module's fixtures
    and parametrized cases.
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
    sessions: async_sessionmaker[AsyncSession],
    name: str,
    *,
    enriched: bool = False,
) -> Title:
    title = Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=f"{MARK} {name}",
        year=2021,
        enrichment_state=EnrichmentState.ENRICHED if enriched else EnrichmentState.SKELETON,
    )
    async with sessions() as session:
        await PostgresTitleRepository(session).add(title)
        await session.commit()
    return title


async def _given_neighbors(
    sessions: async_sessionmaker[AsyncSession], seed: Title, neighbor: Title
) -> None:
    async with sessions() as session:
        await PostgresTitleNeighborRepository(session).replace(
            [seed.id],
            [ScoredNeighbor(title_id=seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
            blend_fingerprint="arranged-by-a-test",
        )
        await session.commit()


async def _jobs(
    sessions: async_sessionmaker[AsyncSession], title: Title
) -> list[dict[str, object]]:
    async with sessions() as session:
        result = await session.execute(
            text("SELECT kind, key, priority, status FROM jobs WHERE key = :key"),
            {"key": str(title.id)},
        )
        return [dict(row) for row in result.mappings().all()]


def _writes(statements: list[str]) -> int:
    return sum(
        statement.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


async def test_the_route_serves_a_stored_row_with_a_poster_through_the_real_wiring(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    seed = await _given_title(sessions, "Row Seed", enriched=True)
    created = [await _given_title(sessions, f"Card {index}") for index in range(8)]
    stored = [one.id for one in reversed(created)]
    poster_card = stored[1]
    recipe = seed_row_recipe(model=settings.llm_model)
    built = datetime.now(UTC)
    async with sessions() as session:
        await PostgresSeedRowRepository(session).save(
            SeedRow(
                seed_title_id=seed.id,
                cards=tuple(
                    SeedRowCard(title_id=one, sources=frozenset({CardSource.NAMED}))
                    for one in stored
                ),
                built_at=built,
                built_recipe=recipe,
                attempted_at=built,
                attempt_outcome=SeedRowOutcome.BUILT,
                attempt_recipe=recipe,
            )
        )
        await PostgresImageRepository(session).replace_for_titles(
            [poster_card],
            [
                Image(
                    title_id=poster_card,
                    kind=ImageKind.POSTER,
                    provider="tmdb",
                    provider_path="/a-poster.jpg",
                    is_primary=True,
                )
            ],
        )
        await session.commit()

    assert len(stored) >= MIN_SEED_ROW_CARDS, "premise: the record is long enough to be served"
    assert stored != [one.id for one in created], "premise: stored order is not creation order"
    body = (await client.get(f"/titles/{seed.id}/similar")).json()
    by_id = {card["title_id"]: card for card in body["cards"]}

    assert body["source"] == "row"
    assert [card["title_id"] for card in body["cards"]] == [str(one) for one in stored]
    assert by_id[str(poster_card)]["artwork"] is not None
    assert by_id[str(stored[0])]["artwork"] is None


async def test_the_neighbours_arrive_as_cards_when_there_is_no_row(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _given_title(sessions, "Seed")
    neighbor = await _given_title(sessions, "A Neighbour")
    await _given_neighbors(sessions, seed, neighbor)

    body = (await client.get(f"/titles/{seed.id}/similar")).json()

    assert body["source"] == "neighbors"
    assert [card["title_id"] for card in body["cards"]] == [str(neighbor.id)]


async def test_no_cards_is_200_and_an_unknown_title_is_404(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    lonely = await _given_title(sessions, "Nothing Like It")

    lonely_response = await client.get(f"/titles/{lonely.id}/similar")
    assert lonely_response.status_code == 200
    assert lonely_response.json() == {"cards": [], "source": "neighbors", "building": False}

    unknown_response = await client.get(f"/titles/{uuid.uuid4()}/similar")
    assert unknown_response.status_code == 404
    assert unknown_response.headers["content-type"] == "application/problem+json"
    assert unknown_response.json()["code"] == "not_found"


async def test_without_a_model_the_route_writes_nothing(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    seed = await _given_title(sessions, "A Read Only Seed", enriched=True)

    statement_counter.clear()
    response = await client.get(f"/titles/{seed.id}/similar")

    assert response.status_code == 200
    assert statement_counter, "no statement was captured, so this proves nothing"
    assert _writes(statement_counter) == 0, statement_counter
    assert response.json()["building"] is False
    assert await _jobs(sessions, seed) == []


async def test_with_a_model_a_due_title_queues_exactly_one_seed_row_job(
    postgres_url: str,
    clean: None,
    sessions: async_sessionmaker[AsyncSession],
    statement_counter: list[str],
) -> None:
    seed = await _given_title(sessions, "A Due Seed", enriched=True)

    async with _serving(_settings(postgres_url, llm_enabled=True)) as with_model:
        statement_counter.clear()
        response = await with_model.get(f"/titles/{seed.id}/similar")

    assert response.status_code == 200
    assert response.json()["building"] is True
    assert _writes(statement_counter) == 1, statement_counter
    jobs = await _jobs(sessions, seed)
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "seed_row"
    assert jobs[0]["key"] == str(seed.id)
    assert jobs[0]["priority"] == 100
    assert jobs[0]["status"] == "pending"


async def test_the_row_the_worker_builds_is_the_row_the_route_stops_building(
    postgres_url: str, clean: None, sessions: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(postgres_url, llm_enabled=True)
    seed = await _given_title(sessions, "A Seed The Worker Builds", enriched=True)
    recalled = [
        await _given_title(sessions, f"Recalled By The Worker {index}", enriched=True)
        for index in range(MIN_SEED_ROW_CARDS)
    ]
    client = FakeLLMClient.returning(
        {"films": [{"title": one.name, "year": one.year} for one in recalled]},
        usages=[usage(model=LEDGER_MODEL)],
    )

    async with _serving(settings) as served:
        before = (await served.get(f"/titles/{seed.id}/similar")).json()
    queued = await _jobs(sessions, seed)
    assert before["building"] is True, "premise: the route asked for a build"
    assert [job["kind"] for job in queued] == ["seed_row"]

    events = FakeEventPublisher()
    async with sessions() as session:
        pipeline = build_pipeline(session, settings)
        service = build_seed_row_service(pipeline, settings, client, FakeMetadataProvider())
        await seed_row_handler(service, events)(Job(kind=JobKind.SEED_ROW, key=str(seed.id)))
    assert len(client.calls) == 1, "premise: the worker built the row"
    assert [event.title_id for event in events.published] == [seed.id]

    async with _serving(settings) as served:
        after = (await served.get(f"/titles/{seed.id}/similar")).json()

    assert after["source"] == "row"
    assert {card["title_id"] for card in after["cards"]} == {str(one.id) for one in recalled}
    assert after["building"] is False
    assert await _jobs(sessions, seed) == queued
