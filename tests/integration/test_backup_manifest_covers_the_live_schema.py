"""The manifest is exhaustive over the schema the migrations actually build.

`usher.db.backup_manifest` is a hand-written classification of every table
in this database, and a hand-written list is exactly the artefact PRD 08's
prose two-column table already proved cannot be maintained by whoever
remembers: M9 added four tables and the section was updated for none of
them, so `row_provider_settings` went unlisted a milestone after the PRD
said it *"belongs in the precious column the day it exists"*, and
`search_queries` was never classified at all. This case is the forcing
function that prose never had.

**It reads `information_schema` rather than `Base.metadata`, and that is
the whole reason it needs Docker.** `Base.metadata` cannot see
`alembic_version` -- Alembic creates that table itself, from
`alembic/env.py`, with no `Table` object anywhere in `src/` -- and it is a
real table in every deployment as well as the one thing K3 stamps its
artifact from. A manifest checked only against the ORM metadata would be
free to forget it. The unit arm checks the metadata half (and so fails on
an M11 model without waiting for a container); this one checks the
database.

It takes `postgres_url` directly rather than `session`, for the reason
`test_migrations.py` states about itself: the schema under test must be
whatever that fixture builds, so this fails if the fixture ever stops
running the real Alembic chain.

⚠️ **A `CREATE VIEW` in `public` would land here as "unclassified".** The
query is deliberately the plain one -- no `table_type = 'BASE TABLE'`
filter -- because a view is a thing a reviewer should have to decide about
rather than a thing this predicate should silently drop. There are none
today: the count below is exactly the 28 tables in `Base.metadata` plus
`alembic_version`.
"""

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
    """Both directions, each with its own message, because they are two
    different mistakes: a table nobody classified is a table a backup
    silently drops, and a table the manifest names that does not exist is a
    manifest describing a schema this deployment no longer has.
    """
    live = await _live_tables(postgres_url)

    # Positive control, and it is two assertions rather than one on
    # purpose. A scan that reads an empty `information_schema` produces the
    # same green as a scan that read the real one, and this repository has
    # been bitten by that shape in five separate tasks; `>= 29` catches a
    # schema-less database and the named anchor catches a query that ran
    # against the wrong `table_schema` and found some other 29 tables.
    assert len(live) >= 29, "the schema scan found nothing"
    assert "llm_calls" in live, "the schema scan read a database that is not this one"

    unclassified = live - set(MANIFEST)
    assert not unclassified, f"tables the manifest does not classify: {sorted(unclassified)}"

    phantom = set(MANIFEST) - live
    assert not phantom, f"tables the manifest classifies that do not exist: {sorted(phantom)}"
