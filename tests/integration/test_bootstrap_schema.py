"""The migration actually builds what the models describe.

tests/integration/test_migrations.py already diffs the whole migrated
schema against Base.metadata, which covers drift. These two cover the
things a diff cannot: that no new trigger appeared, and that the
(tmdb_id, kind) index really lets both TMDb namespaces coexist in one
table.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import build_engine


async def test_bootstrap_tables_added_no_new_triggers(postgres_url: str) -> None:
    """Guards the coupling test_db_models_bootstrap.py describes: an
    updated_at column on any of the three would want a trigger, and would
    break test_migrations.py's exact-set assertion from a different file."""
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        names = {row[0] for row in result}
    await engine.dispose()
    assert names == {
        "trg_sources_set_updated_at",
        "trg_titles_set_updated_at",
        "trg_watch_states_set_updated_at",
    }


async def test_both_tmdb_namespaces_coexist_in_tmdb_ids(session: AsyncSession) -> None:
    """(tmdb_id, kind) as the primary key, exercised rather than inspected:
    26,968 real ids are live in both namespaces, so a single-column key
    would reject this insert and lose half of television."""
    await session.execute(
        text(
            "INSERT INTO tmdb_ids (tmdb_id, kind, original_name, popularity) VALUES "
            "(1, 'movie', 'Some Film', 1.0), (1, 'series', 'Pride', 3.8)"
        )
    )
    result = await session.execute(
        text("SELECT kind FROM tmdb_ids WHERE tmdb_id = 1 ORDER BY kind")
    )
    assert [row[0] for row in result] == ["movie", "series"]
