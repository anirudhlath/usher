"""PostgresRowProviderSettingsRepository against real Postgres.

The contract suite runs here unchanged; the two cases below are the ones the
in-memory fake cannot express by construction -- a real primary-key row count,
and durable persistence across a real commit boundary.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.row_provider_settings_repository_contract import (
    RowProviderSettingsRepositoryContract,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.row_provider_settings import PostgresRowProviderSettingsRepository


class TestPostgresRowProviderSettingsRepository(RowProviderSettingsRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresRowProviderSettingsRepository:
        return PostgresRowProviderSettingsRepository(session)


async def _row_count(session: AsyncSession, slug: str) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM row_provider_settings WHERE slug_prefix = :slug"),
        {"slug": slug},
    )
    return int(result.scalar_one())


async def test_re_setting_the_same_slug_writes_one_row_not_two(session: AsyncSession) -> None:
    """The physical claim `ON CONFLICT (slug_prefix) DO UPDATE` makes, and one
    the in-memory fake's dict cannot get wrong by construction: two writes for
    one slug, the second flipping the value, must still leave exactly one row.
    Deleting the `DO UPDATE` clause does not merely fail to collapse the two
    writes -- the second bare `INSERT` raises a primary-key violation before a
    row count is even reachable, which is the louder and more useful failure
    `test_setting_the_same_slug_twice_upserts_rather_than_duplicating` (the
    shared contract) already catches; this case additionally proves the count
    on the one implementation that can.
    """
    repository = PostgresRowProviderSettingsRepository(session)

    await repository.set_enabled("curated", enabled=False)
    await repository.set_enabled("curated", enabled=True)

    assert await _row_count(session, "curated") == 1


async def test_a_write_is_invisible_to_a_second_session_until_the_caller_commits(
    postgres_url: str,
) -> None:
    """**The standing rule, stated as behaviour rather than as a docstring
    sentence.** `set_enabled` flushes and never commits, so the row it wrote
    is this session's own uncommitted work until the caller commits it --
    `PostgresBulkCatalogRepository.bulk_load_window`'s precedent
    (`tests/integration/test_bulk_repository.py`), reached here from the write
    side of a single-statement repository rather than from a multi-statement
    window.

    Two independent sessions, built off their own engine rather than the
    per-test `session` fixture: that fixture's whole isolation model is a
    transaction the test harness itself rolls back, so calling `.commit()` on
    it here would leave a row behind for every later test in this
    session-scoped container. A second, genuinely separate session is also the
    only way to tell "committed" apart from "merely visible within the same
    still-open transaction" -- reading back through the *same* session would
    pass even if `set_enabled` had never flushed at all.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as first:
            await PostgresRowProviderSettingsRepository(first).set_enabled("curated", enabled=False)

            # Same session: read-your-own-write is visible before commit.
            assert await _row_count(first, "curated") == 1

            # A second, independent session sees nothing yet.
            async with factory() as second:
                assert await _row_count(second, "curated") == 0

            await first.commit()

        # A third session, opened only after the commit, sees the row.
        async with factory() as third:
            assert await _row_count(third, "curated") == 1
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                text("DELETE FROM row_provider_settings WHERE slug_prefix = :slug"),
                {"slug": "curated"},
            )
            await cleanup.commit()
        await engine.dispose()
