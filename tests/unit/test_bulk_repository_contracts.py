"""The bulk contracts, run against the in-memory doubles. No Docker.

tests/integration/test_bulk_repository.py runs the identical assertions
against Postgres.
"""

import pytest

from tests.contract.bulk_catalog_repository_contract import BulkCatalogRepositoryContract
from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.ports.repository import BulkCatalogRepository


class TestFakeBulkCatalogRepository(BulkCatalogRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeBulkCatalogRepository:
        return FakeBulkCatalogRepository()

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.popularity(imdb_id)

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        """Vacuously true: this fake has no index to suspend. Asserted
        anyway so the contract case is not skipped for one implementation
        and enforced for the other."""
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.window_depth == 0


class TestFakeImportRunRepository(ImportRunRepositoryContract):
    @pytest.fixture
    def runs(self) -> FakeImportRunRepository:
        return FakeImportRunRepository()
