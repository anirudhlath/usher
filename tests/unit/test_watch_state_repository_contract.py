"""The shared contract, against the in-memory implementation.

Half of a pair. Every `COALESCE` case here passes for a reason that has
nothing to do with the code under test -- Python's `if value is not None` is
naturally that shape -- so this file proves the assertions are *expressible*
and `tests/integration/test_watch_state_repository.py` proves they *bite*.
"""

import uuid

import pytest

from tests.contract.watch_state_repository_contract import WatchStateRepositoryContract
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.ids import new_id


class TestFakeWatchStateRepository(WatchStateRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeWatchStateRepository:
        return FakeWatchStateRepository()

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def episode_id(self) -> uuid.UUID:
        return new_id()
