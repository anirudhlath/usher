"""Behaviour every `TitleEmbeddingRepository.nearest_known` must satisfy."""

import math
import uuid
from collections.abc import Sequence
from typing import Protocol

from tests.fakes.embedding import planted_pair
from usher.domain.enums import EnrichmentState
from usher.domain.ids import new_id
from usher.ports.repository import TitleEmbeddingRepository

_FLOOR = 500


class Plant(Protocol):
    """Seed one title with a vector, a vote count and an enrichment state."""

    async def __call__(
        self,
        vector: Sequence[float] | None,
        *,
        votes: int | None,
        state: EnrichmentState = EnrichmentState.ENRICHED,
        title_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...


def at(angle: float) -> list[float]:
    """The unit vector `angle` radians from the seed's, which `at(0.0)` is."""
    return planted_pair(angle)[1]


def distance(angle: float) -> float:
    """Cosine distance from the seed, which only ever grows with the angle up to pi."""
    return 1.0 - math.cos(angle)


class TitleEmbeddingNearestKnownContract:
    """Titles close to a seed, among those the catalog knows enough about.

    The Postgres arm is also asserted on its exact scan and its settings, which a dict
    has neither of: `tests/integration/test_search_repository.py`.
    """

    async def test_close_titles_come_back_nearest_first(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """Planted farthest first, so neither insertion order nor id order is the answer."""
        assert distance(0.3) < distance(0.6) < distance(0.9), "the premise: the angles order"
        seed = await plant(at(0.0), votes=_FLOOR)
        far = await plant(at(0.9), votes=_FLOOR)
        near = await plant(at(0.3), votes=_FLOOR)
        middle = await plant(at(0.6), votes=_FLOOR)
        assert [far, near, middle] != [near, middle, far], "the premise: ids do not order them"

        assert await repo.nearest_known(seed, min_votes=_FLOOR, limit=10) == [near, middle, far]

    async def test_a_title_under_the_vote_floor_is_left_out(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """One vote short of the floor and nearer than the title that clears it."""
        assert distance(0.2) < distance(0.5), "the premise: the title under the floor is nearer"
        seed = await plant(at(0.0), votes=_FLOOR)
        await plant(at(0.1), votes=None)
        await plant(at(0.2), votes=_FLOOR - 1)
        clears = await plant(at(0.5), votes=_FLOOR)

        assert await repo.nearest_known(seed, min_votes=_FLOOR, limit=10) == [clears]

    async def test_unknown_votes_count_as_zero(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """The same title the floor of 500 refuses is there when the floor is 0."""
        seed = await plant(at(0.0), votes=_FLOOR)
        unknown = await plant(at(0.4), votes=None)

        assert await repo.nearest_known(seed, min_votes=_FLOOR, limit=10) == []
        assert await repo.nearest_known(seed, min_votes=0, limit=10) == [unknown]

    async def test_the_seed_and_skeletons_are_left_out(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """Each is nearer than the one title that qualifies, and carries votes over the floor."""
        assert distance(0.1) < distance(0.4), "the premise: the skeleton is nearer"
        seed = await plant(at(0.0), votes=_FLOOR * 10)
        await plant(at(0.1), votes=_FLOOR * 10, state=EnrichmentState.SKELETON)
        await plant(None, votes=_FLOOR * 10)
        qualifies = await plant(at(0.4), votes=_FLOOR)
        stub = await plant(at(0.5), votes=_FLOOR, state=EnrichmentState.STUB)

        assert await repo.nearest_known(seed, min_votes=_FLOOR, limit=10) == [qualifies, stub]

    async def test_a_seed_with_no_vector_has_no_close_titles(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """Not the lowest ids, which is what ordering by a `NULL` distance would return."""
        refused = await plant(None, votes=_FLOOR)
        real = await plant(at(0.0), votes=_FLOOR)
        other = await plant(at(0.3), votes=_FLOOR)

        assert await repo.nearest_known(real, min_votes=_FLOOR, limit=10) == [other]
        assert await repo.nearest_known(refused, min_votes=_FLOOR, limit=10) == []
        assert await repo.nearest_known(new_id(), min_votes=_FLOOR, limit=10) == []

    async def test_equal_distances_fall_to_the_lower_id(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """The higher id is planted first, so storage order is not the answer."""
        lower, higher = sorted([new_id(), new_id()])
        seed = await plant(at(0.0), votes=_FLOOR)
        await plant(at(0.5), votes=_FLOOR, title_id=higher)
        await plant(at(0.5), votes=_FLOOR, title_id=lower)

        assert await repo.nearest_known(seed, min_votes=_FLOOR, limit=10) == [lower, higher]

    async def test_the_limit_is_met_even_when_most_nearer_titles_are_under_the_floor(
        self, repo: TitleEmbeddingRepository, plant: Plant
    ) -> None:
        """What an approximate scan with the floor applied afterwards cannot do."""
        limit, under, over = 100, 150, 120
        under_angles = [0.05 + 0.006 * index for index in range(under)]
        over_angles = [1.0 + 0.01 * index for index in range(over)]
        assert under > limit, "the premise: the floor kills more than a whole answer"
        assert max(under_angles) < min(over_angles), "the premise: every dead title is nearer"

        seed = await plant(at(0.0), votes=_FLOOR)
        for angle in under_angles:
            await plant(at(angle), votes=_FLOOR - 1)
        qualifying = [await plant(at(angle), votes=_FLOOR) for angle in over_angles]

        found = await repo.nearest_known(seed, min_votes=_FLOOR, limit=limit)

        assert len(found) == limit
        assert found == qualifying[:limit]
