"""`FakeCuratedRowRepository` against the shared `CuratedRowRepository`
contract.

No Docker, no database. See `tests/fakes/curated_row_repository.py` for the
seven places this half is more forgiving than
`tests/integration/test_curated_row_repository.py`'s -- the first of which is
that `replace_for_user`'s delete scope is structurally correct here, so
`test_a_generation_that_produced_nothing_clears_the_screen` is load-bearing in
the integration run and merely available in this one -- and for the one place
it is *stricter*, which is that a refusal raised after the delete is
observable here and is rolled back out of sight there.
"""

import uuid
from collections.abc import Sequence

import pytest

from tests.contract.curated_row_repository_contract import (
    CuratedRowRepositoryContract,
    CuratedRowSeeder,
)
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from usher.domain.curation import CuratedRow
from usher.domain.ids import new_id


class FakeCuratedRowSeeder(CuratedRowSeeder):
    """Writes straight into the fake's list, bypassing the delete.

    `user()` mints a bare id and stores nothing: there is no `users` table
    here, which is this fake's second recorded divergence.
    """

    def __init__(self, repository: FakeCuratedRowRepository) -> None:
        self._repository = repository

    async def user(self) -> uuid.UUID:
        return new_id()

    async def generation(self, rows: Sequence[CuratedRow]) -> None:
        self._repository.rows.extend(rows)

    async def count(self, user_id: uuid.UUID) -> int:
        return len([row for row in self._repository.rows if row.user_id == user_id])


class TestFakeCuratedRowRepository(CuratedRowRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeCuratedRowRepository:
        return FakeCuratedRowRepository()

    @pytest.fixture
    def seeder(self, repository: FakeCuratedRowRepository) -> FakeCuratedRowSeeder:
        # The *same* object the contract writes through, so the seeded
        # generation and the written one land in one store. Two stores here
        # would make a correct implementation fail rather than a wrong one
        # pass -- the arrangement `FakeCreditRepository` records for `titles`.
        return FakeCuratedRowSeeder(repository)
