"""`FakeSeedRowRepository` against the shared `SeedRowRepository` contract."""

import uuid
from collections.abc import Awaitable, Callable

import pytest

from tests.contract.seed_row_repository_contract import (
    SeedRowRepositoryContract,
    SeedRowSeeder,
    built_row,
)
from tests.fakes.seed_row_repository import FakeSeedRowRepository
from usher.domain.ids import new_id


class FakeSeedRowSeeder(SeedRowSeeder):
    """Mints bare ids and counts the fake's own store reads and writes.

    There is no `titles` table here, which is the fake's first recorded divergence: a
    seed naming no title is stored, where Postgres refuses it.
    """

    def __init__(self, repository: FakeSeedRowRepository) -> None:
        self._repository = repository

    async def title(self) -> uuid.UUID:
        return new_id()

    async def statements_during(self, work: Callable[[], Awaitable[object]]) -> int:
        before = self._repository.statements
        await work()
        return self._repository.statements - before


class TestFakeSeedRowRepository(SeedRowRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeSeedRowRepository:
        return FakeSeedRowRepository()

    @pytest.fixture
    def seeder(self, repository: FakeSeedRowRepository) -> FakeSeedRowSeeder:
        # The same object the contract writes through, so what the counter hears is
        # what the repository did.
        return FakeSeedRowSeeder(repository)


async def test_the_fake_counts_a_save_as_a_statement() -> None:
    """The counter the contract's no-statement case reads has to hear writes too."""
    repository = FakeSeedRowRepository()

    before = repository.statements
    await repository.save(built_row(new_id()))

    assert repository.statements == before + 1
