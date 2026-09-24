"""The manifest is exhaustive over the schema the migrations actually build."""

from sqlalchemy import text

from usher.db.backup_manifest import MANIFEST
from usher.db.base import build_engine


async def _live_tables(postgres_url: str) -> set[str]:
    engine = build_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            return {str(row[0]) for row in result}
    finally:
        await engine.dispose()


async def test_every_table_the_migrations_create_is_classified_by_the_manifest(
    postgres_url: str,
) -> None:
    """Both directions, each with its own message, because they are two different mistakes.

    a table nobody classified is a table a backup silently drops, and a table the
    manifest names that does not exist is a manifest describing a schema this deployment
    no longer has.
    """
    live = await _live_tables(postgres_url)

    # Positive control, and it is two assertions rather than one on purpose.
    assert len(live) >= 29, "the schema scan found nothing"
    assert "llm_calls" in live, "the schema scan read a database that is not this one"

    unclassified = live - set(MANIFEST)
    assert not unclassified, f"tables the manifest does not classify: {sorted(unclassified)}"

    phantom = set(MANIFEST) - live
    assert not phantom, f"tables the manifest classifies that do not exist: {sorted(phantom)}"
