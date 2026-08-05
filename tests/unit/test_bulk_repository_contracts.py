"""The bulk contracts, run against the in-memory doubles. No Docker.

tests/integration/test_bulk_repository.py runs the identical assertions
against Postgres.
"""

import uuid

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

    async def tmdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.tmdb_id(imdb_id)

    async def tvdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.tvdb_id(imdb_id)

    async def name_of(self, repo: BulkCatalogRepository, imdb_id: str) -> str | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.name(imdb_id)

    async def title_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> uuid.UUID | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.title_id(imdb_id)

    async def genome_of(
        self, repo: BulkCatalogRepository, title_id: uuid.UUID
    ) -> tuple[float, ...] | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.genome(title_id)

    async def genome_keys(self, repo: BulkCatalogRepository) -> set[object]:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.genome_keys()

    async def enrich(self, repo: BulkCatalogRepository, imdb_id: str) -> None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        repo.mark_enriched(imdb_id)

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
