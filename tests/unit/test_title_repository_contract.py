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
    TitleRepositoryCandidateContract,
    TitleRepositoryContract,
    TitleRepositoryOwnedContract,
)
from tests.fakes.title_repository import FakeTitleRepository, FakeWatchRow
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


class TestFakeTitleRepositoryCandidates(TitleRepositoryCandidateContract):
    """`list_unwatched_candidates` against the fake. The Postgres half is
    `tests/integration/test_title_repository.py`, and it is the one that can
    fail on the `NOT EXISTS` roll-up, on `NULLS LAST` and on the `&&`
    operator -- all three of which this arm reproduces in Python."""

    @pytest.fixture
    def repo(self) -> FakeTitleRepository:
        return FakeTitleRepository()

    @pytest.fixture
    def own(self, repo: FakeTitleRepository) -> Callable[..., Awaitable[None]]:
        async def _own(
            title_id: uuid.UUID, *, episode: bool = False, available: bool = True
        ) -> None:
            # **An unavailable copy leaves no trace here, and that is the
            # fake's shape rather than a shortcut**: `available_copies` models
            # the *available* half of `media_items`, so a retracted row is
            # simply not in it. The consequence is that
            # `test_a_copy_the_source_has_retracted_does_not_rank_as_owned` is
            # load-bearing in the integration run and merely available in this
            # one, the same asymmetry the episode case has one mixin up.
            if not available:
                repo.available_copies.setdefault(title_id, [])
                return
            copies = repo.available_copies.setdefault(title_id, [])
            copies.append(new_id() if episode else None)

        return _own

    @pytest.fixture
    def watch(self, repo: FakeTitleRepository) -> Callable[..., Awaitable[None]]:
        async def _watch(
            user_id: uuid.UUID,
            *,
            title_id: uuid.UUID | None = None,
            episode_id: uuid.UUID | None = None,
            played: bool = True,
        ) -> None:
            repo.watch_states.append(FakeWatchRow(user_id, title_id, episode_id, played))

        return _watch

    @pytest.fixture
    def episode_of(self, repo: FakeTitleRepository) -> Callable[[uuid.UUID], Awaitable[uuid.UUID]]:
        async def _episode_of(series_id: uuid.UUID) -> uuid.UUID:
            episode_id = new_id()
            repo.episode_series[episode_id] = series_id
            return episode_id

        return _episode_of

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        """A bare id: there is no `users` table here, which is a recorded
        divergence rather than an oversight."""
        return new_id()

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return new_id()
