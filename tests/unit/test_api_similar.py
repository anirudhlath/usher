"""`GET /titles/{id}/similar`, over `SimilarShelfService` and the in-memory household."""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.unit.rows import Library, days_ago
from usher.api.app import create_app
from usher.api.deps import get_similar_shelf_service
from usher.api.dto.home import RowCardResponse
from usher.api.routers import titles as titles_router
from usher.config import Settings
from usher.domain.jobs import JobKind
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.ports.repository import ScoredNeighbor
from usher.services.seed_row_recall import seed_row_recipe
from usher.services.similar_shelf import SimilarShelfService

pytestmark = pytest.mark.anyio

RECIPE = seed_row_recipe(model="m")


@pytest.fixture
def library() -> Library:
    return Library()


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


def _serve(app: FastAPI, library: Library, queue: FakeJobQueue, recipe: str | None) -> None:
    app.dependency_overrides[get_similar_shelf_service] = lambda: SimilarShelfService(
        ctx=library.context(), queue=queue, recipe=recipe
    )


@pytest.fixture
def app(library: Library, queue: FakeJobQueue) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    _serve(built, library, queue, RECIPE)
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _store(library: Library, seed_id: uuid.UUID, card_ids: Sequence[uuid.UUID]) -> None:
    built = days_ago(1)
    await library.seed_rows.save(
        SeedRow(
            seed_title_id=seed_id,
            cards=tuple(
                SeedRowCard(title_id=one, sources=frozenset({CardSource.NAMED})) for one in card_ids
            ),
            built_at=built,
            built_recipe=RECIPE,
            attempted_at=built,
            attempt_outcome=SeedRowOutcome.BUILT,
            attempt_recipe=RECIPE,
        )
    )


async def test_the_body_is_cards_source_and_building_and_nothing_else(
    client: httpx.AsyncClient, library: Library
) -> None:
    seed = await library.title("Seed")
    other = await library.title("Other")
    await library.neighbors.replace(
        [seed],
        [ScoredNeighbor(title_id=seed, neighbor_title_id=other, score=0.5, rank=0)],
        blend_fingerprint="arranged-by-a-test",
    )

    body = (await client.get(f"/titles/{seed}/similar")).json()

    assert set(body) == {"cards", "source", "building"}
    assert len(body["cards"]) == 1, "premise: there is a card to hold the shape against"
    assert all(set(card) == set(RowCardResponse.model_fields) for card in body["cards"])


async def test_a_stored_row_arrives_in_stored_order_with_its_poster_and_watched_mark(
    client: httpx.AsyncClient, library: Library
) -> None:
    seed = await library.title("Seed")
    created = [await library.title(f"Card {index}") for index in range(MIN_SEED_ROW_CARDS)]
    stored = list(reversed(created))
    poster_card, watched = stored[1], stored[2]
    image_id = await library.artwork(poster_card)
    await library.finished(watched, at=days_ago(2))
    await _store(library, seed, stored)

    assert stored != created, "premise: stored order is not creation order"
    body = (await client.get(f"/titles/{seed}/similar")).json()
    by_id = {card["title_id"]: card for card in body["cards"]}

    assert body["source"] == "row"
    assert [card["title_id"] for card in body["cards"]] == [str(one) for one in stored]
    assert by_id[str(poster_card)]["artwork"] == str(image_id)
    assert by_id[str(watched)]["played"] is True
    assert by_id[str(stored[0])]["played"] is False


async def test_a_due_enriched_title_reads_building_over_its_neighbours(
    client: httpx.AsyncClient, library: Library, queue: FakeJobQueue
) -> None:
    seed = await library.title("Seed")
    other = await library.title("Other")
    await library.neighbors.replace(
        [seed],
        [ScoredNeighbor(title_id=seed, neighbor_title_id=other, score=0.5, rank=0)],
        blend_fingerprint="arranged-by-a-test",
    )

    body = (await client.get(f"/titles/{seed}/similar")).json()

    assert body["source"] == "neighbors" and body["building"] is True
    assert [card["title_id"] for card in body["cards"]] == [str(other)]
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


async def test_a_title_with_nothing_similar_is_200_with_no_cards(
    app: FastAPI, client: httpx.AsyncClient, library: Library, queue: FakeJobQueue
) -> None:
    lonely = await library.title("Nothing Like It")
    _serve(app, library, queue, None)

    response = await client.get(f"/titles/{lonely}/similar")

    assert response.status_code == 200
    assert response.json() == {"cards": [], "source": "neighbors", "building": False}
    assert queue.jobs_of(JobKind.SEED_ROW) == []


async def test_an_unknown_title_is_a_404_in_prd_07s_envelope(
    client: httpx.AsyncClient,
) -> None:
    title_id = uuid.uuid4()
    response = await client.get(f"/titles/{title_id}/similar")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/titles/{title_id}/similar"


def test_the_route_holds_no_model_and_no_source_adapter() -> None:
    """Acceptance.

    the route holds no model, no `Embedder` and no `SourceAdapter`, the way
    `tests/unit/test_api_home.py::test_the_home_service_and_every_
    provider_hold_no_source_adapter` asserts it for `HomeService` and every row provider
    -- "it did not raise" is also what a route that swallowed everything produces.

    Walks both `ast.Import` and `ast.ImportFrom`, because a bare `import
    usher.ports.source` is invisible to an `ImportFrom`-only scan.

    **The name scan runs over the module with its docstrings removed**, the
    way `tests/unit/test_rows_curated.py::test_the_curated_module_holds_no_
    llm_client_and_cannot_complete_anything` does it -- this module's own
    docstring argues at length about the `SourceAdapter` it must not
    hold, so a raw `"SourceAdapter" not in source` fails on the *explanation*
    rather than on an import. `ast.unparse` of a docstring-stripped tree keeps
    every identifier and every string annotation and drops only the prose --
    which is the half that matters, since a string annotation needs no import
    at all.
    """
    source = pathlib.Path(inspect.getfile(titles_router)).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "ports.source" not in alias.name, f"imports {alias.name}"
                assert "ports.embedding" not in alias.name, f"imports {alias.name}"
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert "ports.source" not in node.module, f"imports {node.module}"
            assert "ports.embedding" not in node.module, f"imports {node.module}"

    code = ast.unparse(_without_prose(tree))
    assert "get_similar_titles" in code, "the prose strip took the module with it"
    assert "SourceAdapter" not in code, "the titles router names a SourceAdapter"
    assert "Embedder" not in code, "the titles router names an Embedder"
    assert "LLMClient" not in code, "the titles router names an LLMClient"


def _without_prose(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only.

    Same helper as `tests/unit/test_rows_curated.py`'s -- copied rather than
    imported, because importing a test module drags in its fixtures and
    parametrized cases (the reason `tests/fakes/` exists as its own tree).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree
