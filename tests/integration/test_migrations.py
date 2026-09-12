"""Regression coverage for the fixture actually running the real migration."""

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

from tests.integration.conftest import (
    column_set,
    drop_database,
    index_set,
    run_alembic,
    scratch_database,
)
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
        # M7.
        "trg_people_set_updated_at",
        "trg_collections_set_updated_at",
        # M8 adds `curated_rows` and `llm_calls` and this set does not move.
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
    against `b'n'`.

    **Scoped by `conrelid`, and that is a correction M9 forced.** This read
    was `conname LIKE '%episode_id_episodes'`, which was exhaustive when M4
    wrote it and stopped being so the moment `m09a` gave `images` a third
    foreign key to `episodes` -- the case then failed on an entry that is
    correct, in a table it is not about. Widening the expected map instead
    would make an M4 case about ADR-0010's two-way asymmetry silently own
    every future episode FK's delete rule; `images`' three are asserted in
    `test_api_surface_schema.py`, beside the CHECK that decides them."""
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE contype = 'f' AND conname LIKE '%episode_id_episodes' "
                "AND conrelid IN ('public.media_items'::regclass, "
                "                 'public.watch_states'::regclass)"
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
    """`compare_metadata` does not diff a partial index's predicate or a btree's null
    ordering, so `test_migration_matches_the_orm_metadata` is green against an index
    missing either -- and an index missing either is not an error, it just silently
    stops serving the query it was built for.
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
        (
            "ix_curated_rows_user_newest",
            "(user_id, generated_at DESC)",
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


async def test_m10a_moves_field_provenance_keys_in_both_directions(postgres_url: str) -> None:
    """**The one thing `m10a` does that no schema reader in this file can
    see**, and the only statement in it that touches a row.

    `field_provenance` is `field -> provider` and `adapters/tmdb/mapping.py`
    derives its keys from the `Title` field names this revision renames, so a
    revision that moved the columns and left the keys leaves every enriched
    row carrying `"community_rating": "tmdb"` while its *next* enrichment adds
    `"tmdb_vote_average": "tmdb"` beside it -- permanently, because
    `services/enrich.py` merges provenance rather than assigning it. Nothing
    else here would notice: `column_set`, `_constraint_set`, `index_set` and
    `compare_metadata` all read the catalog, and this is data.

    Seeded **below** the revision and read above it, then read again after the
    downgrade, because a key rename is only observable across the boundary.
    The third key is deliberately absent from the seeded row: `upgrade()`
    builds its statement per key, and a wholesale spelling would give that row
    a `tmdb_popularity` entry pointing at nothing -- an invented provenance
    claim, which is exactly the inference this revision's docstring refuses.
    """
    admin, scratch, url = await scratch_database(postgres_url, "prov")
    seeded = new_id()
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09f", direction="up"))
        scratch_engine = build_engine(url)
        try:
            async with scratch_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO titles (id, kind, name, sort_name, field_provenance) "
                        "VALUES (:id, 'movie', :name, :name, CAST(:provenance AS jsonb))"
                    ),
                    {
                        "id": seeded,
                        "name": "A Provenance Carrier",
                        # `overview` is the control: a key nothing renames, so
                        # a statement that rebuilt the whole object instead of
                        # editing three keys drops it and fails here.
                        "provenance": (
                            '{"community_rating": "tmdb", "vote_count": "tmdb", "overview": "tmdb"}'
                        ),
                    },
                )

            await asyncio.to_thread(run_alembic, url, "head")
            async with scratch_engine.connect() as conn:
                above = (
                    await conn.execute(
                        text("SELECT field_provenance FROM titles WHERE id = :id"),
                        {"id": seeded},
                    )
                ).scalar_one()

            assert above == {
                "tmdb_vote_average": "tmdb",
                "tmdb_vote_count": "tmdb",
                "overview": "tmdb",
            }, "the keys moved, the untouched one survived, and no key was invented"

            await asyncio.to_thread(functools.partial(run_alembic, url, "m09f", direction="down"))
            async with scratch_engine.connect() as conn:
                below = (
                    await conn.execute(
                        text("SELECT field_provenance FROM titles WHERE id = :id"),
                        {"id": seeded},
                    )
                ).scalar_one()

            assert below == {
                "community_rating": "tmdb",
                "vote_count": "tmdb",
                "overview": "tmdb",
            }, "the downgrade restored every key it renamed, and only those"
        finally:
            await scratch_engine.dispose()
    finally:
        await drop_database(admin, scratch)


async def test_m10b_gives_an_existing_sync_run_a_zero_position(postgres_url: str) -> None:
    """**The one thing `m10b` does that no schema reader in this file can see**: `ADD
    COLUMN … NOT NULL` against a table that already holds rows.
    """
    admin, scratch, url = await scratch_database(postgres_url, "resume")
    source_id, run_id = new_id(), new_id()
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10a", direction="up"))
        scratch_engine = build_engine(url)
        try:
            async with scratch_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO sources "
                        "(id, kind, name, base_url, credentials_ref, device_id) "
                        "VALUES (:id, 'emby', :name, :url, :ref, :device)"
                    ),
                    {
                        "id": source_id,
                        "name": "A Library That Has Never Finished A Walk",
                        "url": "http://example.invalid",
                        "ref": "unused",
                        "device": "unused",
                    },
                )
                # Any pre-existing row exercises the backfill; `failed` is chosen
                # because it is a *terminal* row, so the assertion below cannot be
                # confused with anything the resume logic does.
                await conn.execute(
                    text(
                        "INSERT INTO sync_runs (id, source_id, kind, status) "
                        "VALUES (:id, :source_id, 'watch_state', 'failed')"
                    ),
                    {"id": run_id, "source_id": source_id},
                )
                # The premise: the column really is absent below the
                # revision, so what is read back above it was written by
                # `m10b.upgrade()` and not by this INSERT.
                assert "position" not in await column_set(url, "sync_runs")

            await asyncio.to_thread(run_alembic, url, "head")
            async with scratch_engine.connect() as conn:
                backfilled = (
                    await conn.execute(
                        text("SELECT position FROM sync_runs WHERE id = :id"), {"id": run_id}
                    )
                ).scalar_one()

            assert backfilled == 0, (
                "a row that predates the column reads back at the top of the walk"
            )
        finally:
            await scratch_engine.dispose()
    finally:
        await drop_database(admin, scratch)


async def test_a_full_down_and_up_cycle_restores_every_index(postgres_url: str) -> None:
    """`downgrade base` then `upgrade head`, on a throwaway database, with the index set
    compared before and after.
    """
    admin, scratch, url = await scratch_database(postgres_url, "cycle")
    try:
        await asyncio.to_thread(run_alembic, url, "head")
        before = await index_set(url)

        # One step back first, which is what an operator rolling back the last migration
        # actually runs -- and the only state in which a forgotten `drop_index`, or (for
        # `ffc`) a forgotten `create_index` in its own `downgrade`, is observable at
        # all.
        await asyncio.to_thread(run_alembic, url, "-1")
        # **Asserted against whatever the current head actually reverses**, so every new
        # migration breaks this block and has to re-point it. Sixteen landings,
        # sixteen loud breaks -- `test_db_migration_status.py` reds if that count
        # and the chain on disk disagree, here or in `db-and-sql.md`.
        at_m10e_columns = await column_set(url, "sync_runs")
        assert "error_code" not in at_m10e_columns, "error_code should not exist below m10f"
        # The premise, for the reason the `m09a` stop below records: an empty
        # column set satisfies the absence above, so without this the block
        # would pass at any depth at which `sync_runs` had ceased to exist.
        assert at_m10e_columns, "the premise: `sync_runs` still exists at `m10e`"

        # **A named stop at `m10d`, holding `m10e`'s one.** Displaced from the
        # `-1` half the moment `m10f` became head.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10d", direction="down"))
        at_m10d_indexes = await index_set(url)
        assert "ix_title_embeddings_model_name" not in at_m10d_indexes, (
            "ix_title_embeddings_model_name should not exist below m10e"
        )
        # The premise, for the reason the `m09a` stop below records: an index
        # set that had lost `title_embeddings` entirely satisfies the absence
        # above for a reason that has nothing to do with `m10e.downgrade()`.
        assert "ix_title_embeddings_hnsw" in at_m10d_indexes, (
            "the premise: `title_embeddings` still exists at `m10d`"
        )

        # **A named stop at `m10c`, holding `m10d`'s one.** Displaced from the
        # `-1` half the moment `m10e` became head, and displaced *because it
        # had teeth*: `-1`-from-`m10e` lands on `m10d`'s applied state, where
        # the index is present and `not in` is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10c", direction="down"))
        at_m10c_indexes = await index_set(url)
        assert "ix_title_neighbors_computed_at" not in at_m10c_indexes
        # The premise, on the same terms: an index set that had lost
        # `title_neighbors` entirely satisfies the absence above for a reason
        # that has nothing to do with `m10d.downgrade()`.
        assert "pk_title_neighbors" in at_m10c_indexes, (
            "the premise: `title_neighbors` still exists at `m10c`"
        )

        # **A named stop at `m10b`, holding `m10c`'s five.** Displaced from the `-1`
        # half the moment `m10d` became head, and displaced *because they had teeth*:
        # `-1`-from-`m10d` landed on `m10c`'s applied state, where all five are present
        # and every `not in` below is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10b", direction="down"))
        at_m10b_columns = await column_set(url, "search_queries")
        assert "surface" not in at_m10b_columns, "surface should not exist below m10c"
        assert "tier" not in at_m10b_columns, "tier should not exist below m10c"
        assert at_m10b_columns, "the premise: `search_queries` still exists at `m10b`"
        at_m10b_indexes = await index_set(url)
        assert "ix_search_queries_at" not in at_m10b_indexes
        assert "ix_llm_calls_at" not in at_m10b_indexes
        assert "ix_llm_calls_generation_id" not in at_m10b_indexes
        assert "pk_llm_calls" in at_m10b_indexes, "the premise: `llm_calls` still exists at `m10b`"

        # **A named stop at `m10a`, holding `m10b`'s one.** Displaced from the `-1` half
        # the moment `m10c` became head, and displaced *because it had teeth*:
        # `-1`-from-`m10c` lands on `m10b`'s applied state, where `sync_runs.position`
        # is present and `not in` is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10a", direction="down"))
        at_m10a_columns = await column_set(url, "sync_runs")
        assert "position" not in at_m10a_columns, "position should not exist below m10b"
        assert at_m10a_columns, "the premise: `sync_runs` still exists at `m10a`"

        # **A named stop at `m09f`, holding `m10a`'s seven.** Displaced from the `-1`
        # half the moment `m10b` became head, and displaced *because they had teeth*:
        # `-1`-from-`m10b` lands on `m10a`'s applied state, where the renames have
        # happened and every `not in` below is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09f", direction="down"))
        at_m09f_columns = await column_set(url, "titles")
        for new in ("tmdb_vote_average", "tmdb_vote_count", "tmdb_popularity"):
            assert new not in at_m09f_columns, f"{new} should not exist below m10a"
        for old in ("community_rating", "vote_count", "popularity"):
            assert old in at_m09f_columns, f"{old} should be back below m10a"
        # The two added columns, asserted separately: a `downgrade()` that
        # reversed the renames and forgot the drops satisfies every line above.
        assert "imdb_num_votes" not in at_m09f_columns
        assert "imdb_average_rating" not in at_m09f_columns

        at_m09f_constraints = await _constraint_set(url, "titles")
        assert "ck_titles_community_rating_range" in at_m09f_constraints
        assert "ck_titles_tmdb_vote_average_range" not in at_m09f_constraints

        # **A named stop at `m09e`, holding `m09f`'s four.** Displaced from the
        # `-1` half the moment `m10a` became head, and displaced *because they
        # had teeth*: `-1`-from-`m10a` lands on `m09f`'s applied state, where
        # `attstorage` is `p` and `== "e"` is false on all three columns.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09e", direction="down"))
        for table, column in (
            ("title_embeddings", "embedding"),
            ("user_taste", "centroid"),
            ("genome_scores", "relevance"),
        ):
            assert await _column_storage(url, table, column) == "e", (
                f"{table}.{column} should be back to pgvector's EXTERNAL default here"
            )
        assert "ix_title_embeddings_hnsw" in await index_set(url)

        # **A named stop at `m09d`, holding `m09e`'s three.** Displaced from the
        # `-1` half the moment `m09f` became head, and displaced *because they
        # had teeth*: `-1`-from-`m09f` lands on `m09e`'s applied state, where
        # both columns are already 1024 wide and `== "halfvec(384)"` is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09d", direction="down"))
        assert await _column_type(url, "title_embeddings", "embedding") == "halfvec(384)"
        assert await _column_type(url, "user_taste", "centroid") == "halfvec(384)"
        assert "ix_title_embeddings_hnsw" in await index_set(url)

        # **A named stop at `m09c`, holding `m09d`'s five.** Displaced from the `-1`
        # half the moment `m09e` became head, and displaced *because they had teeth*:
        # `-1`-from-`m09e` lands on `m09d`'s applied state, where every one of these
        # artefacts is present and every `not in` above was false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09c", direction="down"))
        at_m09c = await index_set(url)
        assert "ix_credits_source_natural_key" not in at_m09c
        assert "ix_people_imdb_id" not in at_m09c
        assert "source" not in await column_set(url, "credits")
        people_columns = await column_set(url, "people")
        assert "imdb_id" not in people_columns
        # The pre-existing column, asserted present in the same breath: a
        # `downgrade()` that dropped `tmdb_id` instead would satisfy the line
        # above and leave `people` unable to identify anybody.
        assert "tmdb_id" in people_columns
        assert "ck_people_imdb_id_not_empty" not in await _constraint_set(url, "people")

        # **A second named stop, at `m09a`, and it exists because `m09c`'s artefacts are
        # not observable at the deep one.** `m09c` alters `images`, and `images` is
        # created by `m09a` -- so at `fe1d40c8b7a3` the table is gone and
        # `column_set(url, "images")` is the empty set, which makes a column assertion
        # there vacuous in one direction and false in the other.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09a", direction="down"))
        at_m09a = await index_set(url)
        # `m09c`'s four, displaced from the `-1` half the moment `m09d` became head --
        # and displaced *because they had teeth*: `uq_images_owner_provider_path` failed
        # loudly on the first run with `m09d` present, which is the eighth landing in a
        # row to do so.
        assert "uq_images_owner_provider_path" not in at_m09a
        images_columns = await column_set(url, "images")
        assert "provider_path" not in images_columns
        assert "remote_url" in images_columns
        images_constraints = await _constraint_set(url, "images")
        assert "ck_images_provider_path_not_empty" not in images_constraints
        assert "ck_images_remote_url_not_empty" in images_constraints
        # The premise for all six: `images` still exists here. An empty column
        # set satisfies every absence above, so without this the block would
        # pass at any depth below `m09a` while asserting nothing.
        assert images_columns, "the premise: `images` still exists at `m09a`"

        # Then down to the revision *below* `ff`, which is where M7 group E's two index
        # changes become observable -- `ffa` sits between head and them now, and `-1`
        # alone no longer reaches them.
        await asyncio.to_thread(
            functools.partial(run_alembic, url, "fe1d40c8b7a3", direction="down")
        )
        stepped = await index_set(url)
        # `ffa`'s, `ffb`'s and `ffc`'s own artefacts, checked here rather than after
        # `-1`.
        assert "ix_titles_popularity" in stepped
        assert "pk_genome_scores" not in stepped
        # `m08a`'s two, displaced from the `-1` half the moment `m08b` became head.
        assert "pk_curated_rows" not in stepped
        assert "pk_llm_calls" not in stepped
        # `m08b`'s one, displaced from the `-1` half the moment `m09a` became head --
        # and it is displaced *because it had teeth*, not because it stopped having
        # them: it failed loudly on the first run with `m09a` present, which is the
        # sixth landing in a row to do so.
        assert "pk_genome_tags" not in stepped
        # `m09a`'s five, displaced from the `-1` half the moment `m09c` became head --
        # and displaced *because they had teeth*: `pk_images` failed loudly on the first
        # run with `m09c` present, which is the seventh landing in a row to do so.
        assert "pk_images" not in stepped
        assert "pk_search_queries" not in stepped
        assert "pk_row_provider_settings" not in stepped
        assert "pk_title_search_names" not in stepped
        # The fifth is not a fifth table.
        assert "ix_titles_name_lower_prefix" not in stepped
        assert "blend_fingerprint" not in await column_set(url, "title_neighbors")
        assert "ix_watch_states_user_recent" not in stepped
        assert "ix_media_items_recently_added" not in stepped
        assert "ix_watch_states_user_played" in stepped

        await asyncio.to_thread(run_alembic, url, "base")
        await asyncio.to_thread(run_alembic, url, "head")
        after = await index_set(url)
        assert after == before, sorted(before ^ after)
        assert "ix_watch_states_user_recent" in after
        assert "ix_media_items_recently_added" in after
        assert "ix_watch_states_user_played" not in after
    finally:
        await drop_database(admin, scratch)


async def _column_type(url: str, table: str, column: str) -> str:
    """One column's rendered type, typmod included -- `halfvec(1024)`.

    The third sibling, added for `m09e`, which is the first head that changes
    a column's **type** rather than adding or dropping one. `column_set`
    cannot see it: `embedding` is in that set before and after, so a
    `downgrade()` replaced by `pass` passes every assertion the name-only
    reader can make.

    `format_type` rather than `information_schema.columns`, and the difference
    is the whole point: `data_type` for a pgvector column is `USER-DEFINED`
    and `character_maximum_length` is NULL, so the standard view knows the
    column exists and nothing about its width. `pg_attribute.atttypmod` is
    where a `halfvec`'s dimension lives, and `format_type` is what renders it
    back into the spelling a migration wrote.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = :table "
                    "AND a.attname = :column AND NOT a.attisdropped"
                ),
                {"table": table, "column": column},
            )
            return str(rows.scalar_one())
    finally:
        await engine.dispose()


async def _column_storage(url: str, table: str, column: str) -> str:
    """One column's `pg_attribute.attstorage` -- `p` PLAIN, `e` EXTERNAL.

    The fourth sibling, added for `m09f`, which changes *only* this. Every
    other reader in this file answers identically on both sides of that
    revision: the name is in `column_set`, the type and typmod are unchanged
    for `_column_type`, and the index is in `index_set`. So without this a
    `downgrade()` body replaced by `pass` is invisible.

    It is also the only schema fact in this file the ORM does not model.
    SQLAlchemy has no storage concept and `compare_metadata` does not look at
    `attstorage`, so `test_migration_matches_the_orm_metadata` reports no drift
    either way -- which is exactly why the property needs a case of its own
    rather than being left to the autogenerate comparison.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT a.attstorage::text FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = :table "
                    "AND a.attname = :column AND NOT a.attisdropped"
                ),
                {"table": table, "column": column},
            )
            return str(rows.scalar_one())
    finally:
        await engine.dispose()


async def _constraint_set(url: str, table: str) -> set[str]:
    """One table's constraint names, of every kind.

    The third sibling of `index_set` and `column_set`, and it exists for the
    same reason spelled one artefact further out: a migration that **renames a
    constraint** is invisible to both of the others, and the whole-chain
    `base`/`head` round trip cannot see a `downgrade()` that forgot the rename
    either, because `base` drops the table and `head` rebuilds it clean. So a
    mis-named CHECK left behind by a partial downgrade would survive every
    other case in this file.

    Reads `pg_constraint` rather than `information_schema.table_constraints`,
    which reports a NOT NULL as a constraint with a generated name and would
    make the set churn.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:table AS regclass)"),
                {"table": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()
