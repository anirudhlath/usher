"""`GET /titles/{id}/similar`, over `SimilarityService` and port fakes.

**Two providers are overridden, `get_similarity_service` and
`get_title_repository`, and nothing about the service is stubbed.** The real
`SimilarityService` runs over `FakeTitleNeighborRepository`/
`FakeTitleEmbeddingRepository`/`FakeTitleRepository`, so a case here is
exercising the route, the DTO and the service's own ordering and staleness
arithmetic on one path, not a stub of them.

**Every id below is a fixed `uuid.UUID(int=...)` where order matters, for the
reason `test_services_similar.py` gives:** a `Title.id` defaults to a
monotonic UUIDv7, so leaving two neighbours to mint in creation order would
make "stored rank order" and "id order" and "insertion order" agree by
accident and prove nothing about which one the route actually used.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import get_similarity_service, get_title_repository
from usher.api.routers import titles as titles_router
from usher.config import Settings
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.repository import ScoredNeighbor
from usher.services.similar import SimilarityService, blend_fingerprint

# Rank 0, the lower score -- the correct "stored order" answer.
_LOW_SCORE_NEIGHBOR = uuid.UUID(int=0x2)
# Rank 1, the higher score *and* the smaller id -- a route that re-sorted on
# `score` desc, or one that fell back to `ORDER BY id`, both put this one
# first. Only the stored-rank spelling puts it second.
_HIGH_SCORE_NEIGHBOR = uuid.UUID(int=0x1)


async def _commit() -> None:
    return None


def _title(name: str, *, id_: uuid.UUID | None = None) -> Title:
    if id_ is None:
        return Title(kind=TitleKind.MOVIE, name=name, sort_name=name)
    return Title(id=id_, kind=TitleKind.MOVIE, name=name, sort_name=name)


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def neighbors() -> FakeTitleNeighborRepository:
    return FakeTitleNeighborRepository()


@pytest.fixture
def embeddings(titles: FakeTitleRepository) -> FakeTitleEmbeddingRepository:
    return FakeTitleEmbeddingRepository(catalog=titles)


@pytest.fixture
def similarity(
    embeddings: FakeTitleEmbeddingRepository,
    neighbors: FakeTitleNeighborRepository,
    titles: FakeTitleRepository,
) -> SimilarityService:
    return SimilarityService(embeddings, neighbors, titles, _commit)


@pytest.fixture
def app(titles: FakeTitleRepository, similarity: SimilarityService) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_repository] = lambda: titles
    built.dependency_overrides[get_similarity_service] = lambda: similarity
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_seed_whose_rows_predate_the_running_blend_is_reported_stale(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    neighbors: FakeTitleNeighborRepository,
) -> None:
    """The failing test named in the plan. Plants a seed's rows under a
    fingerprint that is not `blend_fingerprint()`, with the positive control
    that a seed stamped under the *running* blend reports `stale: false` --
    so the case cannot pass by a route that always answers one value."""
    stale_seed = await _seed(titles, "Stale Seed")
    fresh_seed = await _seed(titles, "Fresh Seed")
    neighbor = await _seed(titles, "A Neighbour")

    await neighbors.replace(
        [stale_seed.id],
        [ScoredNeighbor(title_id=stale_seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
        blend_fingerprint="not-the-running-blend",
    )
    await neighbors.replace(
        [fresh_seed.id],
        [ScoredNeighbor(title_id=fresh_seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
        blend_fingerprint=blend_fingerprint(),
    )

    stale_body = (await client.get(f"/titles/{stale_seed.id}/similar")).json()
    fresh_body = (await client.get(f"/titles/{fresh_seed.id}/similar")).json()

    assert stale_body["stale"] is True
    assert fresh_body["stale"] is False


async def test_stale_reads_count_stale_scoped_to_this_seed_not_the_whole_table(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    neighbors: FakeTitleNeighborRepository,
) -> None:
    """Mutation target named in the plan: reading `count_stale` whole-table
    rather than seed-scoped. With one genuinely stale seed in the table, a
    whole-table read would report `stale: true` for the fresh seed too --
    the same two rows as the case above, asked from the other seed's route."""
    stale_seed = await _seed(titles, "Stale Seed Two")
    fresh_seed = await _seed(titles, "Fresh Seed Two")
    neighbor = await _seed(titles, "Its Neighbour")

    await neighbors.replace(
        [stale_seed.id],
        [ScoredNeighbor(title_id=stale_seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
        blend_fingerprint="an-old-blend",
    )
    await neighbors.replace(
        [fresh_seed.id],
        [ScoredNeighbor(title_id=fresh_seed.id, neighbor_title_id=neighbor.id, score=0.5, rank=0)],
        blend_fingerprint=blend_fingerprint(),
    )

    fresh_body = (await client.get(f"/titles/{fresh_seed.id}/similar")).json()
    assert fresh_body["stale"] is False


async def test_neighbors_render_in_stored_rank_order_never_resorted_on_score(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    neighbors: FakeTitleNeighborRepository,
) -> None:
    """Acceptance: the body carries the stored order, never re-sorted on
    `score` in the route. The distractor is deliberate: the rank-0 neighbour
    has the *lower* score and the *larger* id, so a route that re-sorted
    descending by score, or one that fell back to `ORDER BY id`, both put the
    rank-1 neighbour first -- only the stored-rank spelling matches."""
    seed = await _seed(titles, "The Seed")
    low_score = _title("Ranked First, Scored Lower", id_=_LOW_SCORE_NEIGHBOR)
    high_score = _title("Ranked Second, Scored Higher", id_=_HIGH_SCORE_NEIGHBOR)
    await titles.add(low_score)
    await titles.add(high_score)

    # The premises this ordering case rests on, asserted rather than assumed.
    assert _HIGH_SCORE_NEIGHBOR < _LOW_SCORE_NEIGHBOR, "id order must disagree with rank order"

    await neighbors.replace(
        [seed.id],
        [
            ScoredNeighbor(
                title_id=seed.id, neighbor_title_id=_LOW_SCORE_NEIGHBOR, score=0.2, rank=0
            ),
            ScoredNeighbor(
                title_id=seed.id, neighbor_title_id=_HIGH_SCORE_NEIGHBOR, score=0.9, rank=1
            ),
        ],
        blend_fingerprint=blend_fingerprint(),
    )
    assert 0.9 > 0.2, "score order must disagree with rank order"

    body = (await client.get(f"/titles/{seed.id}/similar")).json()
    rendered_ids = [row["id"] for row in body["neighbors"]]
    assert rendered_ids == [str(_LOW_SCORE_NEIGHBOR), str(_HIGH_SCORE_NEIGHBOR)]


async def test_computed_at_null_and_an_empty_neighbor_list_are_distinguishable(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    neighbors: FakeTitleNeighborRepository,
) -> None:
    """Acceptance: `computed_at: null` (never computed) and an empty result
    list (this title has no neighbours) are distinguishable on the wire.

    Two arrangements, asserted in one case because they are the same claim
    seen from both sides: with the artefact never built at all, `computed_at`
    is `null` *and* the list is empty. With the artefact built for some
    *other* seed -- so `computed_at` is a real timestamp -- a seed with no
    rows of its own still answers an empty list, and `computed_at` does not
    collapse to `null` just because this seed has nothing."""
    never_built = await _seed(titles, "Never Built")
    never_built_body = (await client.get(f"/titles/{never_built.id}/similar")).json()
    assert never_built_body["computed_at"] is None
    assert never_built_body["neighbors"] == []

    built_seed = await _seed(titles, "Has Rows Elsewhere")
    its_neighbor = await _seed(titles, "Its Own Neighbour")
    lonely = await _seed(titles, "Lonely Title")
    await neighbors.replace(
        [built_seed.id],
        [
            ScoredNeighbor(
                title_id=built_seed.id, neighbor_title_id=its_neighbor.id, score=0.5, rank=0
            )
        ],
        blend_fingerprint=blend_fingerprint(),
    )

    lonely_body = (await client.get(f"/titles/{lonely.id}/similar")).json()
    assert lonely_body["computed_at"] is not None
    assert lonely_body["neighbors"] == []


async def test_a_title_with_no_neighbours_is_200_not_404(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    lonely = await _seed(titles, "Nothing Like It")
    response = await client.get(f"/titles/{lonely.id}/similar")
    assert response.status_code == 200
    assert response.json()["neighbors"] == []


async def test_an_unknown_title_is_a_404_in_prd_07s_envelope(
    client: httpx.AsyncClient,
) -> None:
    title_id = uuid.uuid4()
    response = await client.get(f"/titles/{title_id}/similar")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/titles/{title_id}/similar"


def test_the_route_holds_no_embedder_and_no_source_adapter() -> None:
    """Acceptance: the route holds no `Embedder` and no `SourceAdapter`, the
    way `tests/unit/test_api_home.py::test_the_home_service_and_every_
    provider_hold_no_source_adapter` asserts it for `HomeService` and every
    row provider -- "it did not raise" is also what a route that swallowed
    everything produces. Walks both `ast.Import` and `ast.ImportFrom`, because
    a bare `import usher.ports.source` is invisible to an `ImportFrom`-only
    scan.

    **The name scan runs over the module with its docstrings removed**, the
    way `tests/unit/test_rows_curated.py::test_the_curated_module_holds_no_
    llm_client_and_cannot_complete_anything` does it -- this module's own
    docstring (M5's) argues at length about the `SourceAdapter` it must not
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


def _without_prose(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only.

    Same helper as `tests/unit/test_rows_curated.py`'s -- copied rather than
    imported, because importing a test module drags in its fixtures and
    parametrized cases (the reason `tests/fakes/` exists as its own tree)."""
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


async def _seed(titles: FakeTitleRepository, name: str) -> Title:
    title = _title(name)
    await titles.add(title)
    return title
