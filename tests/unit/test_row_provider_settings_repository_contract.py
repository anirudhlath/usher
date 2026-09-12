"""The row-provider-settings contract against the in-memory double. No Docker."""

import pytest

from tests.contract.row_provider_settings_repository_contract import (
    RowProviderSettingsRepositoryContract,
)
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository


class TestFakeRowProviderSettingsRepository(RowProviderSettingsRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeRowProviderSettingsRepository:
        return FakeRowProviderSettingsRepository()
