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
        # M8 adds `curated_rows` and `llm_calls` and this set does not move.
        # Both are write-once artefacts -- a curated row is replaced
        # wholesale, an `llm_calls` row records something that already
        # happened -- so neither has an `updated_at` for a trigger to own.
        # `db/models/curation.py` says so on both tables, because the
        # tempting edit is to add one "for consistency" and it would fail
        # here rather than there.
        #
        # M9's `m09a` adds four tables and this set still does not move, with
        # a different reason each, all of them precedents already in this
        # comment: `images` is replaced wholesale per owner (`credits`', which
        # has no `updated_at` at all for exactly that reason);
        # `search_queries` records something that already happened
        # (`llm_calls`'); `title_search_names` is replaced per
        # `(title_id, kind)` (`credits`' again); and
        # `row_provider_settings`' one writer -- the admin route -- sets
        # `updated_at` explicitly on every statement (`jobs`').
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

    `ix_curated_rows_user_newest` is M8's, and its `DESC` is the one entry
    here that is **not** plan-observable -- measured on
    `pgvector/pgvector:pg17` at 30,000 rows, an ascending index answers
    `ORDER BY generated_at DESC` with an `Index Scan Backward` at the same
    cost, because a btree is bidirectional and the leading column is fixed by
    equality. It is declared, and pinned here, for what a *wrong* direction
    costs later: `ffc` dropped `ix_titles_popularity` for exactly that, and
    the day this read grows a second ordering key the direction stops being
    free. So this assertion pins a declaration rather than a plan, and says
    so -- the alternative is a comment nothing checks.

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

    **Head is `m09f` and the `-1` half is re-pointed at its artefacts.** ⚠️ This
    docstring said `m09c` and "seventh" until 2026-08-14 while the body below
    said `m09f` and "tenth" — three heads updated the inline comment and not the
    prose above it, which is the half a reader reads first. The
    previous spelling asserted `m09a`'s four primary keys were *absent*, which
    held because `-1`-from-`m09a` ran `m09a.downgrade()` and dropped its four
    tables. `-1`-from-`m09c` runs `m09c.downgrade()` instead and stops at the
    `m09a` state, where all four are present -- so the inherited assertion
    **failed, loudly and immediately**, and it was run and watched to fail
    before it was touched (`AssertionError: assert 'pk_images' not in {...}`).
    That is the **tenth** landing in a row to do so (`ffa`, `ffb`, `ffc`,
    `m08a`, `m08b`, `m09a`, `m09c`, `m09d`, `m09e`, `m09f`).

    `m09c` creates no table. It does three things and needs an assertion per
    artefact *kind*, which is the "one per table" rule generalised to a head
    that alters one:

    - a unique **constraint**, `uq_images_owner_provider_path`, which carries
      an index of the same name and so is visible to `_index_set`;
    - a **column rename**, `remote_url` -> `provider_path`, visible only to
      `_column_set`;
    - a **constraint rename**, `ck_images_remote_url_not_empty` ->
      `..._provider_path_not_empty`, visible to neither, which is why
      `_constraint_set` exists. That one is worth spelling out: a
      `downgrade()` that forgot it would leave a CHECK named for a column that
      no longer exists, and **the whole-chain `base`/`head` half cannot see
      it** -- `base` drops the table and `head` rebuilds it clean, exactly the
      blind spot `_column_set`'s own docstring records for a column-only
      migration.

    `m09a`'s five assertions have moved into the revision-pinned block below,
    where revision ids do not drift -- displaced *because they had teeth*, on
    the first run with `m09c` present.

    That is the general case rather than this migration's luck, and it is
    worth stating because the opposite was written here first and was wrong:
    **an inherited `-1` assertion that had teeth cannot survive a new head.**
    Having teeth *means* being true at the state `-1` lands on and false at
    the head's own state -- that is what "observes the head's `downgrade()`"
    is -- and a new head makes `-1` land on exactly the state where it is
    false. The direction of the assertion has nothing to do with it: `ffc`'s
    was positive (`in`) and broke; `ffb`'s was negative (`not in`) and broke
    too, because `-1`-from-`ffc` lands at the `ffb` state where
    `blend_fingerprint` is present. Seven landings, seven loud breaks (`ffa`,
    `ffb`, `ffc`, `m08a`, `m08b`, `m09a`, `m09c`) -- the same seven the
    paragraph above counts, which is the point of stating the number in both
    places. **So the
    alarm to watch for is a `-1` half that stays
    green after a new migration**, which means the assertion it inherited
    never had teeth. `.claude/rules/db-and-sql.md` carries the measurement.

    The displaced assertion has moved into the revision-pinned block below,
    where revision ids do not drift.
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
        # **Asserted against whatever the current head actually reverses**, so
        # every new migration breaks this block and has to re-point it. That
        # is the design rather than a defect: the assertion is only doing its
        # job while it is false at the head's own state, which is precisely
        # what makes it fail the moment `-1` starts landing there. Group F
        # re-pointed it for `ffa`, `af64ba2` for `ffb`, M7 Task 36 for `ffc`,
        # M8 Task 8 for `m08a`, M8 Task 19 for `m08b`, M9 Task M1 for `m09a`,
        # M9 Task C2 for `m09c`, T4R for `m09d`. It is cheaper than a step count, which keeps
        # passing for the wrong reason instead of failing for the right one.
        #
        # **The direction of the assertion does not decide this.** `m09c`
        # creates a constraint and renames a column, so its artefacts are
        # asserted *absent* and the pre-rename name *present*; `ffc` dropped
        # an index so its artefact was asserted present. Both spellings break
        # for the same reason when a head lands on them -- verified against
        # the real chain, see this test's docstring. You do not get to pick
        # the direction; the head's own `downgrade()` does.
        #
        # **One assertion per artefact kind**, which is the rule `m08a` needed
        # per *table* generalised to a head that alters one. `m09c` reverses
        # three things and each is invisible to the other two's reader: a
        # unique constraint (an index, so `_index_set`), a column rename
        # (`_column_set`), and a CHECK's rename (`_constraint_set`, which
        # exists for this -- see that helper).
        #
        # The mutation this block catches is a `downgrade()` body replaced by
        # `pass`, which no other case in this suite can see -- the shared
        # schema is built by one `upgrade head` and never goes down, and the
        # whole-chain `base` round trip below drops every table anyway.
        stepped_back = await _index_set(url)
        # `m09f`'s artefacts, re-pointed here the moment it became head — the
        # tenth landing in a row to do this, and the second where neither
        # `_index_set` nor `_column_set` can carry the assertion.
        #
        # **`m09f` changes a *storage mode*, which is the least visible thing a
        # migration in this project has ever changed.** Nothing about the
        # column's name, type, width, nullability, constraints or indexes
        # moves — `_column_set`, `_column_type` and `_index_set` all answer
        # identically on both sides of it, so every reader this file had before
        # today is blind to the entire revision and a `downgrade()` replaced by
        # `pass` passes all of them. What moves is `pg_attribute.attstorage`,
        # `e` at `m09e` and `p` at head, and `_column_storage` exists for that.
        for table, column in (
            ("title_embeddings", "embedding"),
            ("user_taste", "centroid"),
            ("genome_scores", "relevance"),
        ):
            assert await _column_storage(url, table, column) == "e", (
                f"{table}.{column} should be back to pgvector's EXTERNAL default here"
            )
        # The index in the same breath, because `VACUUM FULL` rebuilds it with
        # the table: a `downgrade()` that reset the storage and lost the graph
        # would satisfy every assertion above and leave the semantic lane
        # without an index, which nothing else in this suite would notice —
        # pgvector answers the same query by sequential scan.
        assert "ix_title_embeddings_hnsw" in stepped_back

        # **A named stop at `m09d`, holding `m09e`'s three.** Displaced from the
        # `-1` half the moment `m09f` became head, and displaced *because they
        # had teeth*: `-1`-from-`m09f` lands on `m09e`'s applied state, where
        # both columns are already 1024 wide and `== "halfvec(384)"` is false.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09d", direction="down"))
        assert await _column_type(url, "title_embeddings", "embedding") == "halfvec(384)"
        assert await _column_type(url, "user_taste", "centroid") == "halfvec(384)"
        assert "ix_title_embeddings_hnsw" in await _index_set(url)

        # **A named stop at `m09c`, holding `m09d`'s five.** Displaced from the
        # `-1` half the moment `m09e` became head, and displaced *because they
        # had teeth*: `-1`-from-`m09e` lands on `m09d`'s applied state, where
        # every one of these artefacts is present and every `not in` above was
        # false. Nine landings, nine loud breaks.
        #
        # `m09d` is a creating head, so the direction is `not in` -- three
        # artefact kinds, one assertion each, because none is observable
        # through another's reader.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09c", direction="down"))
        at_m09c = await _index_set(url)
        assert "ix_credits_source_natural_key" not in at_m09c
        assert "ix_people_imdb_id" not in at_m09c
        assert "source" not in await _column_set(url, "credits")
        people_columns = await _column_set(url, "people")
        assert "imdb_id" not in people_columns
        # The pre-existing column, asserted present in the same breath: a
        # `downgrade()` that dropped `tmdb_id` instead would satisfy the line
        # above and leave `people` unable to identify anybody.
        assert "tmdb_id" in people_columns
        assert "ck_people_imdb_id_not_empty" not in await _constraint_set(url, "people")

        # **A second named stop, at `m09a`, and it exists because `m09c`'s
        # artefacts are not observable at the deep one.** `m09c` alters
        # `images`, and `images` is created by `m09a` -- so at
        # `fe1d40c8b7a3` the table is gone and `_column_set(url, "images")` is
        # the empty set, which makes a column assertion there vacuous in one
        # direction and false in the other. That was measured rather than
        # reasoned: moving these four assertions straight into the block below
        # failed on `assert 'remote_url' in set()`.
        #
        # So a displaced assertion moves to **the shallowest revision at which
        # its artefact still exists**, not automatically to the deep stop. The
        # general form for the next head that alters an existing table rather
        # than creating one: `-1` proves your own `downgrade()`, and the
        # previous head's proof needs a stop above whatever created the thing
        # it altered.
        #
        # `m09a` is a revision id and not a step count, for the reason the
        # deep stop gives: every migration added later shifts what `-2` means.
        await asyncio.to_thread(functools.partial(run_alembic, url, "m09a", direction="down"))
        at_m09a = await _index_set(url)
        # `m09c`'s four, displaced from the `-1` half the moment `m09d` became
        # head -- and displaced *because they had teeth*:
        # `uq_images_owner_provider_path` failed loudly on the first run with
        # `m09d` present, which is the eighth landing in a row to do so. Three
        # artefact kinds, and both directions on the rename for the reason the
        # `-1` block used to give: a `downgrade()` that dropped the column
        # rather than renaming it back satisfies the absence and leaves
        # `images` a column short.
        assert "uq_images_owner_provider_path" not in at_m09a
        images_columns = await _column_set(url, "images")
        assert "provider_path" not in images_columns
        assert "remote_url" in images_columns
        images_constraints = await _constraint_set(url, "images")
        assert "ck_images_provider_path_not_empty" not in images_constraints
        assert "ck_images_remote_url_not_empty" in images_constraints
        # The premise for all six: `images` still exists here. An empty column
        # set satisfies every absence above, so without this the block would
        # pass at any depth below `m09a` while asserting nothing.
        assert images_columns, "the premise: `images` still exists at `m09a`"

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
        # `ffa`'s, `ffb`'s and `ffc`'s own artefacts, checked here rather than
        # after `-1`. These targets are **revision ids**, so unlike the
        # step-back above they do not drift when a migration lands on top --
        # which is precisely why `ffb`'s column assertion moved here the
        # moment `ffc` became head, and why `ffc`'s index assertion moved here
        # the moment `m08a` did. `ffc.downgrade()` recreates
        # `ix_titles_popularity` (wrong declaration and all -- a downgrade
        # restores the schema it reversed, not a better one), and reaching
        # `fe1d40c8b7a3` runs it, so this is the same assertion the `-1` half
        # used to make and it is still exercising `ffc`.
        assert "ix_titles_popularity" in stepped
        assert "pk_genome_scores" not in stepped
        # `m08a`'s two, displaced from the `-1` half the moment `m08b` became
        # head. One assertion per table, for the reason that block records:
        # a `downgrade()` that drops `curated_rows` and forgets `llm_calls`
        # passes a check naming only the first, and `llm_calls` carries no
        # index beyond its primary key.
        #
        # There is deliberately no assertion on `ix_curated_rows_user_newest`.
        # It would be **strictly redundant**: an index cannot outlive its
        # table, so that name is present exactly when `pk_curated_rows` is.
        # Correspondingly, deleting the explicit `op.drop_index` from `m08a`'s
        # `downgrade()` is an equivalent mutation -- `drop_table` takes the
        # index either way -- and that line's own comment says so rather than
        # claiming this block covers it.
        assert "pk_curated_rows" not in stepped
        assert "pk_llm_calls" not in stepped
        # `m08b`'s one, displaced from the `-1` half the moment `m09a` became
        # head -- and it is displaced *because it had teeth*, not because it
        # stopped having them: it failed loudly on the first run with `m09a`
        # present, which is the sixth landing in a row to do so.
        # `genome_tags` ships no index beyond its primary key -- deliberately,
        # `genome_scores`' precedent -- so `pk_genome_tags` is the whole of
        # what stands for it.
        assert "pk_genome_tags" not in stepped
        # `m09a`'s five, displaced from the `-1` half the moment `m09c` became
        # head -- and displaced *because they had teeth*: `pk_images` failed
        # loudly on the first run with `m09c` present, which is the seventh
        # landing in a row to do so. One assertion per table, four tables; a
        # `downgrade()` that drops three of four passes a check naming only
        # the first.
        assert "pk_images" not in stepped
        assert "pk_search_queries" not in stepped
        assert "pk_row_provider_settings" not in stepped
        assert "pk_title_search_names" not in stepped
        # The fifth is not a fifth table. `ix_titles_name_lower_prefix` sits on
        # `titles`, which survives every step above `a8a0e10ff464`, so it is
        # the one artefact `m09a` creates that no `drop_table` collects, and
        # deleting its `op.drop_index` is observable here and nowhere else.
        # `ix_images_title_id` and its two siblings are the redundant kind ruled
        # out below, and so is `ix_title_search_names_name_lower_prefix`: none
        # can fail independently of its own table's primary key.
        assert "ix_titles_name_lower_prefix" not in stepped
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


async def _column_type(url: str, table: str, column: str) -> str:
    """One column's rendered type, typmod included -- `halfvec(1024)`.

    The third sibling, added for `m09e`, which is the first head that changes
    a column's **type** rather than adding or dropping one. `_column_set`
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
    revision: the name is in `_column_set`, the type and typmod are unchanged
    for `_column_type`, and the index is in `_index_set`. So without this a
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

    The third sibling of `_index_set` and `_column_set`, and it exists for the
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
