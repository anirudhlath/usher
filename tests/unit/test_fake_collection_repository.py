"""`FakeCollectionRepository` against the shared `CollectionRepository`
contract.

No Docker, no database. See tests/fakes/collection_repository.py for the five
places this half is more forgiving -- the first of which is that the
`kind = 'movie'` filter is an `if` here and a `WHERE` clause there, which is
the one place these two implementations fail identically under the same
mutation.
"""

import uuid

import pytest

from tests.contract.collection_repository_contract import (
    CollectionRepositoryContract,
    CollectionSeeder,
)
from tests.fakes.collection_repository import FakeCollectionRepository, SeededMediaItem
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id


class FakeCollectionSeeder(CollectionSeeder):
    """Writes into the fake's catalog affordances.

    `order` records insertion order because `list_owned`'s real statement
    orders a collection's members by `release_date NULLS LAST, year NULLS
    LAST, title_id` -- there is no release date here, so insertion order is
    the stand-in and the contract asserts on the member *set* rather than on
    its sequence. Recorded rather than left implicit: a case that asserted the
    sequence would be asserting this fake's convenience, not the port's
    promise.
    """

    def __init__(self, repository: FakeCollectionRepository) -> None:
        self._repository = repository

    def _title(self, kind: TitleKind) -> uuid.UUID:
        title_id = new_id()
        self._repository.catalog.kinds[title_id] = kind
        self._repository.catalog.order.append(title_id)
        return title_id

    async def movie(self) -> uuid.UUID:
        return self._title(TitleKind.MOVIE)

    async def series(self) -> uuid.UUID:
        return self._title(TitleKind.SERIES)

    async def own(
        self, title_id: uuid.UUID, *, available: bool = True, as_episode: bool = False
    ) -> None:
        self._repository.catalog.media_items.append(
            SeededMediaItem(
                title_id=title_id,
                episode_id=new_id() if as_episode else None,
                available=available,
            )
        )

    async def collection_of(self, title_id: uuid.UUID) -> uuid.UUID | None:
        return self._repository.catalog.collection_ids.get(title_id)


class TestFakeCollectionRepository(CollectionRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeCollectionRepository:
        return FakeCollectionRepository()

    @pytest.fixture
    def seeder(self, repository: FakeCollectionRepository) -> FakeCollectionSeeder:
        return FakeCollectionSeeder(repository)
