"""The shared contract, against the in-memory implementation.

Half of a pair, and the weaker half in one specific way: every name+year case
here passes because Python's `name.lower()` agrees with `lower(name)` by
construction. Only `tests/integration/test_title_match_repository.py` can tell
a query that uses `ix_titles_name_lower_year` from one that returns the same
rows by seq-scanning 1,271,138 of them.
"""

import uuid

import pytest

from tests.contract.title_match_repository_contract import (
    TitleCatalog,
    TitleMatchRepositoryContract,
)
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from usher.domain.enums import EnrichmentState, TitleKind


class _FakeCatalog(TitleCatalog):
    """Seeds through the same object the contract reads from -- there is only
    one store, so a read that disagreed with a write would have nowhere to
    hide."""

    def __init__(self, repository: FakeTitleMatchRepository) -> None:
        self._repository = repository

    async def given_title(
        self,
        *,
        kind: TitleKind,
        name: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        enrichment_state: EnrichmentState = EnrichmentState.SKELETON,
    ) -> uuid.UUID:
        return await self._repository.given_title(
            kind=kind,
            name=name,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            tvdb_id=tvdb_id,
            enrichment_state=enrichment_state,
        )


class TestFakeTitleMatchRepository(TitleMatchRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeTitleMatchRepository:
        return FakeTitleMatchRepository()

    @pytest.fixture
    def catalog(self, repository: FakeTitleMatchRepository) -> TitleCatalog:
        return _FakeCatalog(repository)
