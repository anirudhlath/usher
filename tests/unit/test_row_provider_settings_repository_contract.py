"""The row-provider-settings contract against the in-memory double. No Docker.

tests/integration/test_row_provider_settings_repository.py runs the identical
assertions against Postgres, plus the two cases only a real session and a real
row count can demonstrate.
"""

import pytest

from tests.contract.row_provider_settings_repository_contract import (
    RowProviderSettingsRepositoryContract,
)
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository


class TestFakeRowProviderSettingsRepository(RowProviderSettingsRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeRowProviderSettingsRepository:
        return FakeRowProviderSettingsRepository()
