"""PostgresBulkCatalogRepository against real Postgres.

Runs the shared contract, plus the cases that only mean anything against a
real database: that the COPY path reaches asyncpg at all, that
bulk_load_window really drops and rebuilds indexes, and that it declines to
when the catalog is non-empty.
"""

import uuid

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.bulk_catalog_repository_contract import (
    SHAWSHANK,
    BulkCatalogRepositoryContract,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.models.source import SourceRow
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.domain.enums import SourceKind
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

    async def tmdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tmdb_id FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def tvdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tvdb_id FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def name_of(self, repo: BulkCatalogRepository, imdb_id: str) -> str | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT name FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        return await _index_names(repo._session) >= _SUSPENDED


async def test_apply_ratings_upsert_tmdb_ids_upsert_crosswalk_accept_empty_batches(
    session: AsyncSession,
) -> None:
    """The shared contract only exercises the empty-batch guard for
    upsert_titles (test_upsert_titles_accepts_an_empty_batch) -- these three
    early-return the same way (`if not rows: return 0`), and were otherwise
    unreached by any test, live or in-memory. Coverage gap found running
    `pytest --cov` during this task's verification pass, closed here rather
    than in the shared contract (tests/contract/), which is not this file's
    to extend."""
    assert await PostgresBulkCatalogRepository(session).apply_ratings([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_tmdb_ids([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_crosswalk([]) == 0


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
            "FROM titles WHERE imdb_id = 'tt99000020'"
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
        text("SELECT name, sort_name FROM titles WHERE imdb_id = 'tt99000020'")
    )
    name, sort_name = result.one()
    assert name == 'A "Quoted" Synthetic Feature'
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


async def test_bulk_load_window_commits_the_callers_own_pending_work(
    postgres_url: str,
) -> None:
    """Pins the documented, deliberate exception to "these flush and return
    counts; they never commit" -- see BulkCatalogRepository.bulk_load_window
    and PostgresBulkCatalogRepository's own docstrings for the full
    rationale and the (rejected) alternatives.

    Deliberately does NOT use the shared `session` fixture every other test
    in this file uses. That fixture binds its session to a connection with
    an externally-managed outer transaction (`conn.begin()`, see
    tests/integration/conftest.py), and SQLAlchemy's own
    `join_transaction_mode` resolves to "rollback_only" for exactly that
    shape: `session.commit()` there ends the session's *logical* transaction
    scope, but the real DBAPI transaction stays open until the fixture's own
    `conn.rollback()` at teardown. That is exactly why this was invisible
    before -- no test written against `session` can observe a real commit
    here, no matter how carefully it's written, which is the coordinator's
    own diagnosis and this test is built to not repeat it. Building a
    session bound directly to the engine instead (the same shape
    production's `deps.get_session` uses) makes `commit()` a real commit,
    the same way tests/integration/test_migrations.py already does when it
    needs to see real, cross-connection state.

    Because this genuinely commits against the same session-scoped Postgres
    container every other integration test shares, it cleans up after
    itself in a `finally` -- the same discipline test_health.py's
    `test_check_migrations_detects_a_mismatch` docstring calls out for this
    exact fixture.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    source_id = uuid.uuid4()
    try:
        async with factory() as session:
            bulk_repo = PostgresBulkCatalogRepository(session)

            # Unrelated pending work on a table bulk_load_window has no
            # business touching: a different repository's write, sent to
            # Postgres (a Core `insert()` takes effect immediately, no ORM
            # flush needed) but never committed by *this* caller. Stands in
            # for "some other repository call earlier on the same session"
            # -- the exact precondition TitleRepository's own docstring
            # already documents as real, not hypothetical, once a session is
            # shared across repositories.
            await session.execute(
                insert(SourceRow).values(
                    id=source_id,
                    kind=SourceKind.EMBY,
                    name="pending source, never committed by this test",
                    base_url="http://example.invalid",
                    credentials_ref="ref",
                    device_id="device",
                )
            )

            # The empty-catalog branch is the one that commits -- assert the
            # precondition explicitly so a future change elsewhere in the
            # suite that leaves a stray title behind fails loudly here
            # rather than silently taking the other branch and passing for
            # the wrong reason.
            assert await bulk_repo.count_titles() == 0

            async with bulk_repo.bulk_load_window():
                pass

            # This test -- the caller -- has still never called
            # session.commit() itself.

        # A second, independent session: the only way to tell "genuinely
        # committed" apart from "merely visible within the same still-open
        # transaction" (read-your-own-writes would pass even if nothing
        # here actually committed).
        async with factory() as fresh:
            result = await fresh.execute(
                text("SELECT count(*) FROM sources WHERE id = :id"), {"id": source_id}
            )
            assert result.scalar_one() == 1, (
                "bulk_load_window no longer commits the caller's pending work. "
                "If this is a deliberate fix (e.g. it now uses its own session "
                "instead of the caller's), update its docstring, the port's "
                "documented precondition on BulkCatalogRepository.bulk_load_window, "
                "and this test together -- don't just delete the assertion."
            )
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(SourceRow).where(SourceRow.id == source_id))
            await cleanup.commit()
        await engine.dispose()
