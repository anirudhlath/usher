"""`FakeGenomeRepository` against the shared `GenomeRepository` contract."""

import uuid
from collections.abc import Sequence

import pytest

from tests.contract.genome_repository_contract import (
    RELEASE_A,
    GenomeRepositoryContract,
    GenomeSeeder,
)
from tests.fakes.genome_repository import FakeGenomeRepository
from usher.domain.ids import new_id
from usher.ports.repository import GenomeVectorRow


class FakeGenomeSeeder(GenomeSeeder):
    def __init__(self, repository: FakeGenomeRepository) -> None:
        self._repository = repository

    async def title(self) -> uuid.UUID:
        title_id = new_id()
        self._repository.titles.add(title_id)
        return title_id

    async def vector(
        self, title_id: uuid.UUID, relevance: tuple[float, ...], *, revision: str = RELEASE_A
    ) -> None:
        self._repository.vectors[title_id] = GenomeVectorRow(
            title_id=title_id, relevance=relevance, genome_revision=revision
        )

    async def tags(self, tags: Sequence[tuple[int, str]], *, revision: str = RELEASE_A) -> None:
        for tag_id, tag in tags:
            self._repository.tags[tag_id] = (tag, revision)


class TestFakeGenomeRepository(GenomeRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeGenomeRepository:
        return FakeGenomeRepository()

    @pytest.fixture
    def seeder(self, repository: FakeGenomeRepository) -> FakeGenomeSeeder:
        return FakeGenomeSeeder(repository)
