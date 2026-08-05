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

import asyncio
import functools
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

from tests.integration.conftest import run_alembic
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
        # M7. Both are written by `INSERT ... ON CONFLICT DO UPDATE` out of a
        # temporary staging table, which `onupdate=` never reaches. There is
        # deliberately **no** `trg_credits_set_updated_at`: `credits` has no
        # `updated_at` column at all, because every write to it is an insert
        # -- a title's credit set is replaced rather than merged, and an
        # upsert cannot express the deletion of a credit that disappeared
        # upstream. If a run demands that seventh name, the model grew an
        # `updated_at` it should not have.
        "trg_people_set_updated_at",
        "trg_collections_set_updated_at",
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


async def test_the_row_read_indexes_carry_the_clauses_that_make_them_work(
    session: AsyncSession,
) -> None:
    """`compare_metadata` does not diff a partial index's predicate or a
    btree's null ordering, so `test_migration_matches_the_orm_metadata` is
    green against an index missing either -- and an index missing either is
    not an error, it just silently stops serving the query it was built for.

    `ix_watch_states_user_recent` without `NULLS LAST` serves the filter and
    cannot supply the order, so Postgres replaces an incremental sort with a
    full one and Continue Watching sorts the household's whole per-user set
    on every home screen. The rows it returns are identical, which is why
    nothing else can see it.

    `ix_media_items_recently_added` without its `WHERE` is a larger index
    over every row including the review queue and every retracted file.
    Correct answers, wrong size, and no test would notice.

    Asserted off `pg_indexes.indexdef` -- what Postgres will actually do --
    rather than off `Base.metadata`, the same discipline
    `test_search_schema.py` applies to `confdeltype`.
    """
    for name, expected in (
        (
            "ix_watch_states_user_recent",
            "(user_id, played, last_played_at DESC NULLS LAST)",
        ),
        (
            "ix_media_items_recently_added",
            "(added_at DESC NULLS LAST) WHERE (available AND (title_id IS NOT NULL))",
        ),
    ):
        result = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
        )
        definition = result.scalar_one_or_none()
        assert definition is not None, f"{name} is declared on the model and not in the database"
        assert expected in str(definition), definition


async def test_the_dropped_watch_state_index_is_gone(session: AsyncSession) -> None:
    """`ix_watch_states_user_played` is replaced rather than supplemented,
    because `(user_id, played, last_played_at DESC NULLS LAST)` is a strict
    prefix superset -- anything the narrow one could serve, the wide one
    serves.

    Two indexes where one suffices is a write cost on every merge of every
    nightly walk -- up to 1,126,789 states -- for no read. Asserted rather
    than assumed because a migration that creates the new one and forgets
    the drop passes every other case in this suite.
    """
    result = await session.execute(
        text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_watch_states_user_played'")
    )
    assert result.scalar_one() == 0


async def test_a_full_down_and_up_cycle_restores_every_index(postgres_url: str) -> None:
    """`downgrade base` then `upgrade head`, on a throwaway database, with
    the index set compared before and after.

    **This exists because a mutation survived without it.** Deleting the
    `op.create_index("ix_watch_states_user_played", ...)` from `ff`'s
    `downgrade()` passes every other case in this file, and it would pass
    forever: the session-scoped schema is built by one `upgrade head` and
    never goes down, so an upgrade-only migration is green in a suite that
    never reverses one. What it costs in production is a schema that is one
    index short of where it started after a rollback -- and `watch_states`
    is the table whose merge path runs a million times a night.

    A throwaway database rather than the shared one, because
    `downgrade base` drops every table and every other test in this run is
    built on the schema it would take with it.

    Deliberately compares the whole index set rather than the two indexes
    this milestone touches: a downgrade that forgets *any* index is the same
    defect, and naming only the new ones would make this case blind to the
    next one.

    **The step-back block has two halves and they answer different
    questions.** `-1` exercises whatever the *current head* is, so it moves
    with the chain and is what catches a brand-new migration whose
    `downgrade()` is a no-op. The named `fe1d40c8b7a3` target below it
    exercises `ff` specifically, which `-1` stopped reaching the moment `ffa`
    landed on top -- the failure this case had on the first run after that,
    and a good illustration of why a step count is the wrong pin.
    """
    admin = postgres_url.rsplit("/", 1)[0]
    scratch = f"cycle_{uuid.uuid4().hex[:12]}"
    engine = build_engine(f"{admin}/postgres")
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
    finally:
        await engine.dispose()

    url = f"{admin}/{scratch}"
    try:
        await asyncio.to_thread(run_alembic, url, "head")
        before = await _index_set(url)

        # One step back first, which is what an operator rolling back the
        # last migration actually runs -- and the only state in which a
        # forgotten `drop_index`, or (for `ffc`) a forgotten `create_index` in
        # its own `downgrade`, is observable at all. Going straight to `base`
        # drops the tables, and a table takes its indexes with it, so a
        # downgrade that forgets one is invisible from there.
        await asyncio.to_thread(run_alembic, url, "-1")
        # **Asserted against whatever the current head actually reverses**, and
        # that target moves with every migration -- `ffc` *drops* an index on
        # upgrade where `ffb` added a *column* and `ffa` added a table, so the
        # previous spelling (`"blend_fingerprint" not in ...`) silently stopped
        # exercising the head's own `downgrade()` the moment `ffc` landed on
        # top and started failing instead. Group F recorded exactly this for
        # `ffa`, M7 Task 36 for `ffb`; it is the cost of a self-maintaining
        # half, and it is cheaper than a step count that keeps passing for the
        # wrong reason. `ffc.downgrade()` recreates `ix_titles_popularity`, so
        # after one step back it is present again -- the mutation this catches
        # is a `ffc.downgrade` that forgets to.
        assert "ix_titles_popularity" in await _index_set(url)

        # Then down to the revision *below* `ff`, which is where M7 group E's
        # two index changes become observable -- `ffa` sits between head and
        # them now, and `-1` alone no longer reaches them.
        #
        # **Named, not counted in `-N` steps.** Every migration added after
        # this one shifts what `-2` means, so a step count would silently
        # re-point this block at an unrelated revision and keep passing for
        # the wrong reason. A revision id is stable, and reversing more than
        # `ff` on the way there costs these assertions nothing.
        await asyncio.to_thread(
            functools.partial(run_alembic, url, "fe1d40c8b7a3", direction="down")
        )
        stepped = await _index_set(url)
        # `ffa`'s and `ffb`'s own artefacts, checked here rather than after
        # `-1`. These targets are **revision ids**, so unlike the step-back
        # above they do not drift when a migration lands on top -- which is
        # precisely why `ffb`'s column assertion moved here the moment `ffc`
        # became head and the `-1` step-back stopped reaching `ffb`.
        assert "pk_genome_scores" not in stepped
        assert "blend_fingerprint" not in await _column_set(url, "title_neighbors")
        assert "ix_watch_states_user_recent" not in stepped
        assert "ix_media_items_recently_added" not in stepped
        assert "ix_watch_states_user_played" in stepped

        await asyncio.to_thread(run_alembic, url, "base")
        await asyncio.to_thread(run_alembic, url, "head")
        after = await _index_set(url)
        assert after == before, sorted(before ^ after)
        assert "ix_watch_states_user_recent" in after
        assert "ix_media_items_recently_added" in after
        assert "ix_watch_states_user_played" not in after
    finally:
        engine = build_engine(f"{admin}/postgres")
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        await engine.dispose()


async def _column_set(url: str, table: str) -> set[str]:
    """One table's column names. The sibling of `_index_set`, for the
    migrations that add a column rather than an index -- without it, a
    column-only migration's `downgrade()` has nothing that can observe it
    short of the whole-chain `base`/`head` round trip, which passes against a
    no-op downgrade because `base` drops the table anyway."""
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def _index_set(url: str) -> set[str]:
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()
