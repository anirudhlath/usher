"""The image contract against the in-memory double. No Docker.

`tests/integration/test_image_repository.py` runs the identical assertions
against Postgres, plus the cases only a real constraint and a real second
session can demonstrate. See `tests/fakes/image_repository.py` for the five
places this half is more forgiving.

The one case below that is *not* in the shared contract is the statement count,
and it is here rather than there for the reason `rows-and-genome.md` records:
the claim is "one statement per shelf", and against an in-memory dict a
**timing** assertion measures the dict. Counting is the only honest way to see
it, and only the fake can be counted.
"""

import uuid

import pytest

from tests.contract.image_repository_contract import (
    ImageRepositoryContract,
    ImageSeeder,
    image,
)
from tests.fakes.image_repository import FakeImageRepository
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id


class FakeImageSeeder(ImageSeeder):
    """A title id and nothing else.

    The fake enforces no foreign key, so "seeding a title" is minting an id and
    telling the repository it exists — which is the *first* of its recorded
    divergences, and the reason
    `test_an_image_for_a_title_that_does_not_exist_is_a_port_error` lives on
    the Postgres arm alone.
    """

    def __init__(self, repository: FakeImageRepository) -> None:
        self._repository = repository

    async def title(self) -> uuid.UUID:
        title_id = new_id()
        self._repository.known_titles.add(title_id)
        return title_id


class TestFakeImageRepository(ImageRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeImageRepository:
        return FakeImageRepository()

    @pytest.fixture
    def seeder(self, repository: FakeImageRepository) -> FakeImageSeeder:
        return FakeImageSeeder(repository)


async def test_a_whole_shelf_costs_one_statement() -> None:
    """**Counted, not timed.** The port's promise is one statement per shelf
    whatever the shelf's length, and it is the whole reason
    `primary_for_titles` takes a sequence: a shelf is up to thirty cards and
    `GET /home` composes ten of them, so the per-card shape is three hundred
    round trips a screen.

    A timing assertion here would measure a Python dict —
    `rows-and-genome.md`'s four-reads finding — so the fake counts its own
    calls instead, and the wrong implementation this kills is a
    `primary_for_titles` that loops over `title_ids` calling `list_for_title`.
    That one is *correct*, which is what makes a behavioural assertion unable
    to see it.

    The count is asserted at two shelf lengths and asserted **equal**, rather
    than asserted `== 1` once: `== 1` also passes for an implementation that
    happens to answer the first title only, and two lengths giving one number
    is the claim that the cost does not scale.
    """
    repository = FakeImageRepository()
    seeder = FakeImageSeeder(repository)

    one_title = [await seeder.title()]
    thirty = [await seeder.title() for _ in range(30)]
    await repository.replace_for_titles(
        [*one_title, *thirty],
        [
            image(one, f"/card-{index}.jpg", is_primary=True)
            for index, one in enumerate([*one_title, *thirty])
        ],
    )

    repository.reset_calls()
    await repository.primary_for_titles(one_title, ImageKind.POSTER)
    for_one = repository.calls

    repository.reset_calls()
    found = await repository.primary_for_titles(thirty, ImageKind.POSTER)
    for_thirty = repository.calls

    assert len(found) == 30, "the premise: the long call really did answer every title"
    assert for_one == 1
    assert for_thirty == for_one
