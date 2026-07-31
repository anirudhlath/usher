"""The shared contract, against the in-memory implementation.

Half of a pair. `test_a_duplicate_episode_inside_one_batch_is_tolerated` and
its season twin pass here because a dict cannot hold a key twice, which says
nothing about the `SELECT DISTINCT ON` the real one needs to avoid
`CardinalityViolationError` -- see `tests/integration/test_episode_repository.py`.
"""

import uuid

import pytest

from tests.contract.episode_repository_contract import EpisodeRepositoryContract
from tests.fakes.episode_repository import FakeEpisodeRepository
from usher.domain.ids import new_id


class TestFakeEpisodeRepository(EpisodeRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeEpisodeRepository:
        return FakeEpisodeRepository()

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def season_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_season_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_title_id(self) -> uuid.UUID:
        return new_id()
