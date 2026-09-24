"""The shared contract, against the in-memory implementation."""

import uuid

import pytest

from tests.contract.sync_run_repository_contract import SyncRunRepositoryContract
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from usher.domain.ids import new_id


class TestFakeSyncRunRepository(SyncRunRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeSyncRunRepository:
        return FakeSyncRunRepository()

    @pytest.fixture
    def source_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_source_id(self) -> uuid.UUID:
        return new_id()
