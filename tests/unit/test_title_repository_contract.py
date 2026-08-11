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
    TitleRepositoryBrowseContract,
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
            #
            # It returns without touching the store at all. An earlier version
            # wrote an empty list under the title's id, which was a no-op
            # dressed as a record -- `bool([])` is what a title with no entry
            # already answers -- and a line that looks like a write and is not
            # is worse than the absence it models.
            if not available:
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


class TestFakeTitleRepositoryBrowse(TitleRepositoryBrowseContract):
    """`browse`/`browse_facets` against the fake. The Postgres half is
    `tests/integration/test_title_repository.py`, and it is the one that can
    fail on the keyset's `IS NOT DISTINCT FROM` arm, on `NULLS LAST`, on `@>`
    and on `unnest`."""

    @pytest.fixture
    def repo(self) -> FakeTitleRepository:
        return FakeTitleRepository()

    @pytest.fixture
    def own(self, repo: FakeTitleRepository) -> Callable[..., Awaitable[None]]:
        async def _own(
            title_id: uuid.UUID, *, episode: bool = False, available: bool = True
        ) -> None:
            # The candidate arm's fixture, verbatim in shape and for the same
            # reasons -- see its comment. `available=False` leaves no trace
            # because `available_copies` models the *available* half of
            # `media_items` by construction, so the retracted distractor in
            # `test_owned_means_an_available_title_level_copy` is load-bearing
            # in the integration run and merely available in this one.
            #
            # `episode=True` is **not** vacuous here, unlike in the candidate
            # arm: browse's `owned` carries `episode_id IS NULL`, and the list
            # stores `None` for a title-level copy and an episode id for an
            # episode one, so the fake can and does tell the two apart.
            if not available:
                return
            copies = repo.available_copies.setdefault(title_id, [])
            copies.append(new_id() if episode else None)

        return _own
