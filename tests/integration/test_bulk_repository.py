"""PostgresBulkCatalogRepository against real Postgres.

Runs the shared contract, plus the cases that only mean anything against a
real database: that the COPY path reaches asyncpg at all, that
bulk_load_window really drops and rebuilds indexes, and that it declines to
when the catalog is non-empty.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.bulk_catalog_repository_contract import (
    SHAWSHANK,
    BulkCatalogRepositoryContract,
)
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.ports.repository import BulkCatalogRepository

_SUSPENDED = {"ix_titles_sort_name", "ix_titles_name_lower_year"}


async def _index_names(session: AsyncSession) -> set[str]:
    result = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'titles'")
    )
    return {row[0] for row in result}


class TestPostgresBulkCatalogRepositoryContract(BulkCatalogRepositoryContract):
    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresBulkCatalogRepository:
        return PostgresBulkCatalogRepository(session)

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        # repo._session reaches state the port deliberately does not expose.
        # No suppression comment for the private-member access: that ruff
        # code is not in this project's `select` list, and a directive
        # naming a non-selected code trips RUF100 ("unused directive")
        # instead, which *is* selected. Verified against this project's
        # ruff config.
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT popularity FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return float(value) if value is not None else None

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        return await _index_names(repo._session) >= _SUSPENDED


async def test_copy_writes_the_server_default_columns(session: AsyncSession) -> None:
    """The reason TitleRow carries server_defaults at all: the COPY path
    never mentions enrichment_state, field_provenance, keywords,
    spoken_languages, origin_countries, or created_at. Without them this
    insert fails on `null value in column "genres"`."""
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    result = await session.execute(
        text(
            "SELECT enrichment_state, field_provenance, keywords, created_at IS NOT NULL "
            "FROM titles WHERE imdb_id = 'tt0111161'"
        )
    )
    state, provenance, keywords, has_created_at = result.one()
    assert state == "skeleton"
    assert provenance == {}
    assert keywords == []
    assert has_created_at is True


async def test_copy_preserves_embedded_double_quotes(session: AsyncSession) -> None:
    """IMDb's TSVs carry literal `"` in title fields and have no quoting
    mechanism. This asserts the value survives the whole COPY path
    byte-for-byte, which is the other half of the parser-side decision not
    to use csv.reader (see adapters/bulk/imdb.py)."""
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    result = await session.execute(
        text("SELECT name, sort_name FROM titles WHERE imdb_id = 'tt0111161'")
    )
    name, sort_name = result.one()
    assert name == 'The "Shawshank" Redemption'
    assert sort_name == name


async def test_bulk_load_window_suspends_indexes_on_an_empty_catalog(
    session: AsyncSession,
) -> None:
    repo = PostgresBulkCatalogRepository(session)
    async with repo.bulk_load_window():
        assert _SUSPENDED & await _index_names(session) == set()
    assert await _index_names(session) >= _SUSPENDED


async def test_bulk_load_window_declines_on_a_populated_catalog(
    session: AsyncSession,
) -> None:
    """ADR-0005 promises the catalog is browsable while bootstrap runs. On
    a first bootstrap there is nothing to browse, so dropping the two
    ordering indexes is free; on a re-import a browse ordered by name would
    seq-scan for the whole window, so the write cost is accepted instead.
    Delete the count_titles() guard and this fails."""
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    async with repo.bulk_load_window():
        assert await _index_names(session) >= _SUSPENDED
