"""`FakeSearchQueryRepository` against the shared `SearchQueryRepository`
contract.

No Docker, no database. See `tests/fakes/search_query_repository.py` for the
four places this half is more forgiving than
`tests/integration/test_search_query_repository.py`'s -- chiefly that the
fake has no foreign keys and no client-side integer encoder, so the two
`RepositoryConflict` cases about *values* a column cannot hold are
Postgres-only.
"""

import uuid

import pytest

from tests.contract.search_query_repository_contract import (
    SearchQueryLedger,
    SearchQueryRepositoryContract,
    StoredSearchQuery,
)
from tests.fakes.search_query_repository import FakeSearchQueryRepository
from usher.domain.ids import new_id


class FakeSearchQueryLedger(SearchQueryLedger):
    """Reads the fake's own two dicts.

    Bypasses nothing, because there is nothing to bypass: the port has no
    read method on either arm, so `record()`/`record_outcome()` are the only
    writers and the ledger's whole job is to observe. It is a
    `SearchQueryLedger` rather than a direct reach into `repository.rows` so
    that the *same* observation is made on both arms.
    """

    def __init__(self, repository: FakeSearchQueryRepository) -> None:
        self._repository = repository

    async def get(self, query_id: uuid.UUID) -> StoredSearchQuery | None:
        record = self._repository.rows.get(query_id)
        if record is None:
            return None
        clicked_title_id, played = self._repository.outcomes[query_id]
        return StoredSearchQuery(
            id=record.id,
            at=record.at,
            user_id=record.user_id,
            query=record.query,
            mode=record.mode,
            result_count=record.result_count,
            latency_ms=record.latency_ms,
            clicked_title_id=clicked_title_id,
            played=played,
        )

    async def count(self) -> int:
        return len(self._repository.rows)


class TestFakeSearchQueryRepository(SearchQueryRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeSearchQueryRepository:
        return FakeSearchQueryRepository()

    @pytest.fixture
    def ledger(self, repository: FakeSearchQueryRepository) -> FakeSearchQueryLedger:
        # The *same* object the contract writes through -- two stores here
        # would make a correct implementation fail rather than a wrong one
        # pass, `FakeLLMCallRepository`'s arrangement.
        return FakeSearchQueryLedger(repository)

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return new_id()

    async def add_title(self) -> uuid.UUID:
        # No foreign key on this arm, so any id names a legitimate title as
        # far as the fake is concerned -- see the fake's own divergence list.
        return new_id()

    async def add_user(self) -> uuid.UUID:
        # Same reason as `add_title`: no `users` table to insert into, and
        # the scope case needs the id to be *different*, not to exist. The
        # Postgres arm is where "different and real" is the same thing.
        return new_id()
