"""Regression coverage for the fixture actually running the real migration.

`tests/integration/conftest.py` used to build its schema with
`Base.metadata.create_all` -- which only ever sees SQLAlchemy `Table`
metadata, never the hand-written `op.execute(...)` calls the migration
itself needs for triggers and functions (CLAUDE.md's Commands section
already documents that `--autogenerate` is blind to these; the same
blindness applies to `create_all`, for the same reason: neither one runs
Alembic's actual migration script). That meant the Alembic migration chain
was never executed against a live Postgres anywhere in this suite, so it
and `Base.metadata` were free to drift with nothing here to notice.

Both tests take `postgres_url` directly, not `session` -- deliberately: the
schema must come from whatever `postgres_url` itself builds, so that these
tests fail if that fixture ever regresses back to not running the
migration. Both were written and run before the fixture fix, confirming
each fails for the right reason: `test_migration_creates_the_updated_at_triggers`
found no triggers at all (`postgres_url` handed back a schema-less
database -- schema creation used to live entirely in the `session`
fixture, via `Base.metadata.create_all`, which these two tests don't
request), and `test_migration_matches_the_orm_metadata` reported sixteen
`add_table`-shaped diffs for the same reason. Neither failure is
hypothetical or contrived.
"""

from typing import cast

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection

from usher.db.base import Base, build_engine


async def test_migration_creates_the_updated_at_triggers(postgres_url: str) -> None:
    """The three `set_updated_at` triggers are hand-written `op.execute()`
    calls in the migration -- entirely invisible to
    `Base.metadata.create_all`. Their own migration comment calls them
    "what actually guarantees updated_at reflects every write, regardless
    of how it was made", specifically for M2/M4's `ON CONFLICT DO UPDATE`
    bulk paths -- true only if something actually runs the migration that
    creates them, which is exactly what `postgres_url` now does.
    """
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        trigger_names = {row[0] for row in result}
    await engine.dispose()
    assert trigger_names == {
        "trg_sources_set_updated_at",
        "trg_titles_set_updated_at",
        "trg_watch_states_set_updated_at",
    }


async def test_migration_matches_the_orm_metadata(postgres_url: str) -> None:
    """Autogenerate-diffing the *migrated* database against `Base.metadata`
    is what actually proves the hand-maintained migration and the
    SQLAlchemy models it's supposed to mirror haven't drifted apart --
    catching exactly the two categories of change CLAUDE.md already warns
    `--autogenerate` alone is blind to (CHECK constraint bodies, and
    triggers/functions) requires running it against a database the
    migration itself built, not one `create_all` built directly from the
    same models it would be compared against.
    """

    def _diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection)
        # compare_metadata isn't precisely typed by alembic's own stubs --
        # it returns Any -- the cast just pins the shape this test actually
        # relies on (a list, empty when nothing has drifted).
        return cast(list[object], compare_metadata(context, Base.metadata))

    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    await engine.dispose()
    assert diff == []
