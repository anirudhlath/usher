"""The shared contract, against the in-memory implementation.

Half of a pair. Every `COALESCE` case here passes for a reason that has
nothing to do with the code under test -- Python's `if value is not None` is
naturally that shape -- so this file proves the assertions are *expressible*
and `tests/integration/test_watch_state_repository.py` proves they *bite*.

The episode fixtures are wired to one series on purpose: `list_recent` rolls a
watched episode up through `episodes.title_id`, and this fake has no episodes
table, so the mapping is handed to it at construction. `episode_id` and every
id in `episode_ids` belong to `episode_series_id`, exactly as they do against
Postgres.
"""

import uuid

import pytest

from tests.contract.watch_state_repository_contract import (
    WatchStateRepositoryContract,
    WatchStateRepositoryInProgressContract,
)
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.ids import new_id


class TestFakeWatchStateRepository(
    WatchStateRepositoryContract, WatchStateRepositoryInProgressContract
):
    @pytest.fixture
    def episode_series_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def episode_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def episode_ids(self) -> list[uuid.UUID]:
        return [new_id() for _ in range(10)]

    @pytest.fixture
    def repository(
        self,
        episode_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
        episode_series_id: uuid.UUID,
    ) -> FakeWatchStateRepository:
        return FakeWatchStateRepository(
            episode_series={one: episode_series_id for one in (episode_id, *episode_ids)}
        )

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def third_title_id(self) -> uuid.UUID:
        return new_id()
