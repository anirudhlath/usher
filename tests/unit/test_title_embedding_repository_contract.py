"""`FakeTitleEmbeddingRepository` against `TitleEmbeddingNearestKnownContract`."""

import uuid
from collections.abc import Sequence

import pytest

from tests.contract.title_embedding_repository_contract import (
    Plant,
    TitleEmbeddingNearestKnownContract,
    at,
)
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState
from usher.domain.ids import new_id


@pytest.fixture
def catalog() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def repo(catalog: FakeTitleRepository) -> FakeTitleEmbeddingRepository:
    return FakeTitleEmbeddingRepository(catalog=catalog)


@pytest.fixture
def plant(repo: FakeTitleEmbeddingRepository) -> Plant:
    async def _plant(
        vector: Sequence[float] | None,
        *,
        votes: int | None,
        state: EnrichmentState = EnrichmentState.ENRICHED,
        title_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        title = await repo.given(
            title_id or new_id(), vector, tmdb_vote_count=votes, enrichment_state=state
        )
        return title.id

    return _plant


class TestFakeTitleEmbeddingNearestKnown(TitleEmbeddingNearestKnownContract):
    """The Postgres half is `tests/integration/test_search_repository.py`.

    It is the one that can fail on the exact-scan settings, on the join to `titles`
    and on `coalesce`.
    """


async def test_votes_and_state_are_read_from_the_catalog_the_fake_shares(
    catalog: FakeTitleRepository, repo: FakeTitleEmbeddingRepository
) -> None:
    """Two fakes of one table: a write through either is what the other reads."""
    seed = await repo.given(new_id(), at(0.0), tmdb_vote_count=500)
    near = await repo.given(new_id(), at(0.3), tmdb_vote_count=499)
    assert await repo.nearest_known(seed.id, min_votes=500, limit=5) == []

    await catalog.update(near.evolve(tmdb_vote_count=500))

    assert await repo.nearest_known(seed.id, min_votes=500, limit=5) == [near.id]
