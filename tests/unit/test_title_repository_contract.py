"""FakeTitleRepository against the shared TitleRepository contract (see
tests/contract/title_repository_contract.py). No Docker, no database --
this is the unit half of proving the fake and the real, Postgres-backed
PostgresTitleRepository (tests/integration/test_title_repository.py's
TestPostgresTitleRepositoryContract) actually agree.
"""

import uuid
from collections.abc import Awaitable, Callable

import pytest

from tests.contract.title_repository_contract import (
    TitleRepositoryContract,
    TitleRepositoryOwnedContract,
)
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.ids import new_id


class TestFakeTitleRepository(TitleRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeTitleRepository:
        return FakeTitleRepository()


class TestFakeTitleRepositoryOwned(TitleRepositoryOwnedContract):
    """`list_owned_by_tag` against the fake. The Postgres half is
    `tests/integration/test_title_repository.py`."""

    @pytest.fixture
    def repo(self) -> FakeTitleRepository:
        return FakeTitleRepository()

    @pytest.fixture
    def own(self, repo: FakeTitleRepository) -> Callable[..., Awaitable[None]]:
        async def _own(title_id: uuid.UUID, *, episode: bool = False) -> None:
            copies = repo.available_copies.setdefault(title_id, [])
            copies.append(new_id() if episode else None)

        return _own
