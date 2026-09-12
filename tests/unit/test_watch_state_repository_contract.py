"""The shared contract, against the in-memory implementation."""

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
