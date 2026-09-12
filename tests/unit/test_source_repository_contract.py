"""The source-repository contract against the in-memory double. No Docker."""

import pytest

from tests.contract.source_repository_contract import SourceRepositoryContract
from tests.fakes.source_repository import FakeSourceRepository


class TestFakeSourceRepository(SourceRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeSourceRepository:
        return FakeSourceRepository()
