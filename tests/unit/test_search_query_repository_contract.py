"""`FakeSearchQueryRepository` against the shared `SearchQueryRepository` contract."""

import uuid

import pytest

from tests.contract.search_query_repository_contract import (
    ReferenceCounts,
    ReferenceRowCounts,
    SearchQueryLedger,
    SearchQueryRepositoryContract,
    StoredSearchQuery,
)
from tests.fakes.search_query_repository import FakeSearchQueryRepository
from usher.domain.ids import new_id


class FakeSearchQueryLedger(SearchQueryLedger):
    """Reads the fake's own two dicts.

    A `SearchQueryLedger` rather than a direct reach into `repository.rows`, so that
    both arms of the contract make the same observation.
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
            # Read off the record the fake stored rather than defaulted here:
            # a ledger supplying `SEARCH`/`None` of its own would make the
            # suggest case pass on this arm whatever the fake did with them.
            surface=record.surface,
            tier=record.tier,
        )

    async def count(self) -> int:
        return len(self._repository.rows)


class FakeReferenceCounts(ReferenceCounts):
    """The two tables the fake does not have, modelled as constants.

    A divergence rather than a shortcut: `search_queries` being a leaf is a property of
    two foreign keys and this arm has none, so "the prune took no household and no
    title" holds here by construction. The claim is load-bearing only on the Postgres
    arm, where `tests/integration/test_search_query_repository.py` counts real rows. The
    counts are 1 each so the case's premise guard is satisfied honestly.
    """

    async def read(self) -> ReferenceRowCounts:
        return ReferenceRowCounts(users=1, titles=1)


class TestFakeSearchQueryRepository(SearchQueryRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeSearchQueryRepository:
        return FakeSearchQueryRepository()

    @pytest.fixture
    def counts(self) -> FakeReferenceCounts:
        return FakeReferenceCounts()

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
