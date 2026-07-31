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

`test_migration_matches_the_orm_metadata` turns out not to be enough on its
own, which M4 found by mutation rather than by reading: deleting one
`sa.CheckConstraint(...)` line from a migration leaves this whole file
green. `compare_metadata` reports nothing at all for a CHECK constraint
present in `Base.metadata` and absent from the database -- the same
blindness CLAUDE.md records for a *changed* CHECK body, one step further
than it was stated. Since CHECK constraints are the only thing standing
between the bulk `COPY` path and a negative episode number (that path
never constructs a Pydantic model), that gap mattered.
`test_every_check_constraint_in_the_models_exists_in_the_database` closes
it by reading `pg_constraint` directly.
"""

import re
import uuid
from typing import cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import Base, build_engine
from usher.domain.ids import new_id


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
        # M4. Both tables are written by `INSERT ... ON CONFLICT DO UPDATE`
        # from a staging table, so `onupdate=` never fires for them. The
        # other three new tables get none: `jobs`' one writer sets
        # `updated_at` explicitly on every statement, and `sync_runs` and
        # `raw_payloads` have no `updated_at` column at all.
        "trg_seasons_set_updated_at",
        "trg_episodes_set_updated_at",
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


# Postgres rewrites `x BETWEEN a AND b` into `x >= a AND x <= b` on the way
# into `pg_get_constraintdef`, so the comparison below expands it on the
# model side too rather than reporting two false positives forever.
_BETWEEN = re.compile(r"(\w+)\s+BETWEEN\s+(\S+)\s+AND\s+(\S+)", re.IGNORECASE)


def _normalise_check_body(sql: str) -> str:
    """Enough normalisation to compare a hand-written CHECK body against
    what Postgres stores, and no more.

    Postgres re-prints a constraint from its parse tree: it parenthesises
    aggressively, lowercases keywords inconsistently with the source, and
    inserts explicit casts (`''::text`, `(0)::double precision`). None of
    that changes the condition, so all of it is stripped. What is *not*
    stripped is any operator, column name, or literal -- so a loosened bound
    (`>= 0` becoming `>= -1`) still shows up as a difference, which is the
    entire point.
    """
    expanded = _BETWEEN.sub(r"\1 >= \2 AND \1 <= \3", sql).lower()
    without_casts = re.sub(r"::[a-z ]+", "", expanded)
    return re.sub(r"[()\"'\s]", "", without_casts)


async def test_every_check_constraint_in_the_models_exists_in_the_database(
    postgres_url: str,
) -> None:
    """The gap `test_migration_matches_the_orm_metadata` cannot see.

    Verified by mutation: deleting
    `sa.CheckConstraint("episode_number >= 0", ...)` from the M4 migration
    leaves every other test in this file passing, because
    `compare_metadata` does not diff CHECK constraints in either direction.
    This schema deliberately mirrors every Pydantic bound as a CHECK
    precisely so the bulk `COPY` path -- which constructs no Pydantic model
    at all -- cannot store a value the domain model would reject, so a
    constraint the migration forgot is a silent hole in that guarantee.

    Bodies are compared, not just names: CLAUDE.md's original finding was
    that *loosening a bound* produces an empty `pass` migration with no
    warning, and a name-only check would still be green for it."""
    expected = {
        constraint.name: _normalise_check_body(str(constraint.sqltext))
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
            )
        )
        actual = {
            name: _normalise_check_body(body.removeprefix("CHECK ")) for name, body in result.all()
        }
    await engine.dispose()
    assert actual == expected


# --- M4's two new foreign keys --------------------------------------------


async def test_the_new_episode_foreign_keys_carry_the_delete_rule_they_were_given(
    postgres_url: str,
) -> None:
    """Read back off `pg_constraint`, not off `Base.metadata`: `confdeltype`
    is what Postgres will actually do, and it is the whole content of the
    ADR-0010 asymmetry. `n` is SET NULL, `r` is RESTRICT.

    `confdeltype::text` is not decoration -- the column's type is `"char"`,
    which asyncpg hands back as `bytes`, so the uncast comparison fails
    against `b'n'`."""
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE contype = 'f' AND conname LIKE '%episode_id_episodes'"
            )
        )
        rules = {name: rule for name, rule in result.all()}
    await engine.dispose()
    assert rules == {
        "fk_media_items_episode_id_episodes": "n",
        "fk_watch_states_episode_id_episodes": "r",
    }


async def test_both_new_foreign_keys_have_an_index_the_referential_check_can_use(
    postgres_url: str,
) -> None:
    """Every referenced-side DELETE runs a lookup by the *referencing*
    column -- to NULL those rows, or to refuse -- and neither pre-existing
    index can serve it (`uq_media_items_source_external` leads with
    `source_id`, `uq_watch_states_user_episode` with `user_id`). This asserts
    the plan is index-shaped rather than a scan; `enable_seqscan = off`
    forces the planner to reveal whether a usable index exists at all, which
    is the property being claimed. An empty table would otherwise seq-scan
    regardless of how many indexes it has, and prove nothing.

    Neither index was in the M4 plan. The identical argument is already
    written into `db/models/watch.py` for `ix_watch_states_title_id`."""
    probes = [
        ("media_items", "ix_media_items_episode_id"),
        ("watch_states", "ix_watch_states_episode_id"),
    ]
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        for table, index_name in probes:
            # The shape Postgres' own RI trigger uses for ON DELETE SET NULL
            # and ON DELETE RESTRICT.
            result = await conn.execute(
                text(
                    f"EXPLAIN SELECT 1 FROM {table} "  # noqa: S608 -- table is a literal above
                    "WHERE episode_id = '00000000-0000-0000-0000-000000000000' FOR KEY SHARE"
                )
            )
            plan = "\n".join(row[0] for row in result)
            assert index_name in plan, f"{table}: {plan}"
        await conn.rollback()
    await engine.dispose()


async def test_deleting_a_title_cascades_into_its_episodes(session: AsyncSession) -> None:
    """`seasons`/`episodes` CASCADE from `titles` because neither protects
    any user state and both are re-derivable from a cached provider payload.
    Contrast the RESTRICT one test below."""
    title_id, season_id, episode_id = new_id(), new_id(), new_id()
    await _insert_series_tree(session, title_id, season_id, episode_id)

    await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title_id})
    remaining = await session.execute(
        text("SELECT count(*) FROM episodes WHERE id = :id"), {"id": episode_id}
    )
    assert remaining.scalar_one() == 0


async def test_a_titles_cascade_is_refused_when_watch_history_hangs_off_an_episode(
    session: AsyncSession,
) -> None:
    """The two rules composing, which is the point of choosing them
    separately. `titles -> episodes` is CASCADE and `watch_states.episode_id`
    is RESTRICT, so deleting a series whose episodes carry history fails at
    the DELETE two levels down instead of silently destroying that history.
    That is ADR-0010's argument reaching episodes, and it is the reason
    `episode_id` is RESTRICT rather than the CASCADE that would have been
    the shorter diff."""
    title_id, season_id, episode_id = new_id(), new_id(), new_id()
    await _insert_series_tree(session, title_id, season_id, episode_id)
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": user_id, "name": f"viewer-{user_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO watch_states (id, user_id, episode_id, played, origin) "
            "VALUES (:id, :user_id, :episode_id, true, 'source')"
        ),
        {"id": new_id(), "user_id": user_id, "episode_id": episode_id},
    )

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title_id})


async def _insert_series_tree(
    session: AsyncSession, title_id: uuid.UUID, season_id: uuid.UUID, episode_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (:id, 'series', 'A Series', 'A Series')"
        ),
        {"id": title_id},
    )
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season_id, "title_id": title_id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 1, 1)"
        ),
        {"id": episode_id, "title_id": title_id, "season_id": season_id},
    )
