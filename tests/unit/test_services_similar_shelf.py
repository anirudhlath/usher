"""`SimilarShelfService` -- a title page's stored row or neighbours, and the build it queues."""

import uuid
from collections.abc import Sequence

import pytest

from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import Library, days_ago
from usher.domain.enums import EnrichmentState, ImageKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.rows import SimilarSource
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.domain.title import Title
from usher.ports.repository import ScoredNeighbor
from usher.services.seed_row_recall import seed_row_recipe
from usher.services.similar_shelf import SimilarShelfService

pytestmark = pytest.mark.anyio

RECIPE = seed_row_recipe(model="m")
OTHER_RECIPE = seed_row_recipe(model="another")
_FP = "arranged-by-a-test"


def _service(
    library: Library, queue: FakeJobQueue, recipe: str | None = RECIPE
) -> SimilarShelfService:
    return SimilarShelfService(ctx=library.context(), queue=queue, recipe=recipe)


async def _store(
    library: Library,
    seed_id: uuid.UUID,
    card_ids: Sequence[uuid.UUID],
    *,
    age_days: float = 1,
    recipe: str = RECIPE,
) -> None:
    built = days_ago(age_days)
    await library.seed_rows.save(
        SeedRow(
            seed_title_id=seed_id,
            cards=tuple(
                SeedRowCard(title_id=one, sources=frozenset({CardSource.NAMED})) for one in card_ids
            ),
            built_at=built,
            built_recipe=recipe,
            attempted_at=built,
            attempt_outcome=SeedRowOutcome.BUILT,
            attempt_recipe=recipe,
        )
    )


async def _rank(library: Library, seed_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> None:
    await library.neighbors.replace(
        [seed_id],
        [
            ScoredNeighbor(
                title_id=seed_id, neighbor_title_id=one, score=0.9 - 0.01 * rank, rank=rank
            )
            for rank, one in enumerate(ids)
        ],
        blend_fingerprint=_FP,
    )


async def _cards(library: Library, count: int = MIN_SEED_ROW_CARDS) -> list[uuid.UUID]:
    return [await library.title(f"Card {index}") for index in range(count)]


async def test_a_stored_row_is_served_in_stored_order_with_poster_owned_and_watched() -> None:
    library = Library()
    queue = FakeJobQueue()
    seed = await library.title("Seed")
    created = await _cards(library)
    stored = list(reversed(created))
    unowned = await library.title("Not owned", owned=False)
    stored = [*stored[:7], unowned]
    poster_card, watched = stored[1], stored[2]
    image_id = await library.artwork(poster_card)
    await library.finished(watched, at=days_ago(2))
    await _store(library, seed, stored)
    await _rank(library, seed, [await library.title("Other 1"), await library.title("Other 2")])

    assert stored != sorted(stored), "premise: stored order is not insertion order"
    assert stored != [*created[:7], unowned], "premise: stored order differs from creation order"
    shelf = await _service(library, queue).read(seed)

    assert shelf is not None
    assert shelf.source is SimilarSource.ROW
    assert [card.title_id for card in shelf.cards] == stored
    assert {c.title_id: c.artwork for c in shelf.cards} == {
        one: (image_id if one == poster_card else None) for one in stored
    }
    assert {c.title_id for c in shelf.cards if c.played} == {watched}
    assert {c.title_id for c in shelf.cards if not c.owned} == {unowned}


async def test_with_no_stored_row_the_neighbours_are_served_in_rank_order_up_to_twenty() -> None:
    library = Library()
    seed = await library.title("Seed")
    created = [await library.title(f"Neighbour {index}") for index in range(21)]
    ranked = list(reversed(created))
    await _rank(library, seed, ranked)

    assert len(await library.neighbors.list_for(seed, limit=50)) == 21, "premise: 21 exist"
    shelf = await _service(library, FakeJobQueue()).read(seed)

    assert shelf is not None
    assert shelf.source is SimilarSource.NEIGHBORS
    assert [card.title_id for card in shelf.cards] == ranked[:20]


async def test_a_record_holding_only_a_failed_attempt_serves_the_neighbours() -> None:
    library = Library()
    seed = await library.title("Seed")
    await _rank(library, seed, [await library.title("One"), await library.title("Two")])
    await library.seed_rows.save(
        SeedRow(
            seed_title_id=seed,
            attempted_at=days_ago(1),
            attempt_outcome=SeedRowOutcome.TOO_FEW,
            attempt_recipe=RECIPE,
        )
    )

    shelf = await _service(library, FakeJobQueue()).read(seed)

    assert shelf is not None
    assert shelf.source is SimilarSource.NEIGHBORS and len(shelf.cards) == 2


async def test_a_stored_card_the_catalog_no_longer_holds_is_dropped_in_place() -> None:
    library = Library()
    seed = await library.title("Seed")
    stored = await _cards(library, MIN_SEED_ROW_CARDS - 1)
    stored.insert(2, new_id())
    await _store(library, seed, stored)

    shelf = await _service(library, FakeJobQueue()).read(seed)

    assert shelf is not None
    assert [card.title_id for card in shelf.cards] == stored[:2] + stored[3:]


async def test_an_unknown_title_is_none_and_queues_nothing() -> None:
    library = Library()
    queue = FakeJobQueue()

    assert await _service(library, queue).read(new_id()) is None
    assert queue.jobs_of(JobKind.SEED_ROW) == []


@pytest.mark.parametrize(
    ("record", "state", "recipe", "building", "jobs"),
    [
        ("none", EnrichmentState.ENRICHED, RECIPE, True, 1),
        ("none", EnrichmentState.ENRICHED, None, False, 0),
        ("none", EnrichmentState.STUB, RECIPE, False, 0),
        ("built-1d", EnrichmentState.ENRICHED, RECIPE, False, 0),
        ("built-31d", EnrichmentState.ENRICHED, RECIPE, True, 1),
        ("built-other-recipe", EnrichmentState.ENRICHED, RECIPE, True, 1),
        ("too-few-1d", EnrichmentState.ENRICHED, RECIPE, False, 0),
        ("too-few-8d", EnrichmentState.ENRICHED, RECIPE, True, 1),
    ],
)
async def test_a_build_is_queued_exactly_when_due_enriched_and_a_model_is_configured(
    record: str, state: EnrichmentState, recipe: str | None, building: bool, jobs: int
) -> None:
    library = Library()
    queue = FakeJobQueue()
    title_id = await library.title("Seed", enrichment_state=state)
    if record == "built-1d":
        await _store(library, title_id, await _cards(library))
    elif record == "built-31d":
        await _store(library, title_id, await _cards(library), age_days=31)
    elif record == "built-other-recipe":
        await _store(library, title_id, await _cards(library), recipe=OTHER_RECIPE)
    elif record.startswith("too-few"):
        await library.seed_rows.save(
            SeedRow(
                seed_title_id=title_id,
                attempted_at=days_ago(1 if record == "too-few-1d" else 8),
                attempt_outcome=SeedRowOutcome.TOO_FEW,
                attempt_recipe=RECIPE,
            )
        )

    shelf = await _service(library, queue, recipe).read(title_id)

    assert shelf is not None
    assert shelf.building is building
    queued = queue.jobs_of(JobKind.SEED_ROW)
    assert len(queued) == jobs
    if jobs:
        assert queued[0].key == str(title_id)
        assert queued[0].priority == JobPriority.DEMAND


async def test_a_31_day_old_row_is_still_served_while_its_rebuild_is_queued() -> None:
    library = Library()
    queue = FakeJobQueue()
    seed = await library.title("Seed")
    await _store(library, seed, await _cards(library), age_days=31)

    shelf = await _service(library, queue).read(seed)

    assert shelf is not None
    assert shelf.source is SimilarSource.ROW and shelf.building is True
    assert len(shelf.cards) == MIN_SEED_ROW_CARDS
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


async def test_a_second_read_while_the_job_waits_still_says_building_and_queues_no_more() -> None:
    library = Library()
    queue = FakeJobQueue()
    seed = await library.title("Seed")
    service = _service(library, queue)

    first, second = await service.read(seed), await service.read(seed)

    assert first is not None and second is not None
    assert first.building is True and second.building is True
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


async def test_an_unenriched_title_queues_on_the_read_after_it_is_enriched() -> None:
    library = Library()
    queue = FakeJobQueue()
    seed = await library.title("Seed", enrichment_state=EnrichmentState.STUB)
    service = _service(library, queue)

    before = await service.read(seed)
    assert before is not None and before.building is False
    assert queue.jobs_of(JobKind.SEED_ROW) == []

    stored = await library.titles.get(seed)
    assert stored is not None
    await library.titles.update(stored.evolve(enrichment_state=EnrichmentState.ENRICHED))

    after = await service.read(seed)
    assert after is not None and after.building is True
    assert len(queue.jobs_of(JobKind.SEED_ROW)) == 1


class _CountingTitles(FakeTitleRepository):
    def __init__(self) -> None:
        super().__init__()
        self.catalog_reads = 0

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        self.catalog_reads += 1
        return await super().list_by_ids(title_ids)


class _CountingCopies(FakeMediaItemRepository):
    def __init__(self) -> None:
        super().__init__()
        self.ownership_reads = 0

    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        self.ownership_reads += 1
        return await super().owned_title_ids(title_ids)


class _CountingImages(FakeImageRepository):
    def __init__(self) -> None:
        super().__init__()
        self.artwork_reads = 0

    async def primary_for_titles(
        self, title_ids: Sequence[uuid.UUID], kind: ImageKind
    ) -> dict[uuid.UUID, Image]:
        self.artwork_reads += 1
        return await super().primary_for_titles(title_ids, kind)


async def test_the_cards_cost_one_catalog_read_one_ownership_read_and_one_artwork_read() -> None:
    catalog, copies, images = _CountingTitles(), _CountingCopies(), _CountingImages()
    library = Library(titles=catalog, media_items=copies)
    library.images = images
    seed = await library.title("Seed")
    stored = await _cards(library)
    await library.artwork(stored[0])
    await _store(library, seed, stored)
    catalog.catalog_reads = copies.ownership_reads = images.artwork_reads = 0

    shelf = await _service(library, FakeJobQueue()).read(seed)

    assert shelf is not None
    assert len(shelf.cards) == 8, "premise: the reads happened"
    assert catalog.catalog_reads == 1
    assert copies.ownership_reads == 1
    assert images.artwork_reads == 1
