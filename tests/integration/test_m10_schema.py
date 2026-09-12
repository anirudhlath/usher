"""`m10c`'s five artefacts, one assertion each, plus the two things a
catalog reader cannot see.

**One assertion per artefact and not one combined case.** A migration that
ships four of five passes a check naming only the first -- the rule `m08a`
needed per table and `m09c` generalised per artefact kind
(`tests/integration/test_migrations.py`). The five are `search_queries.surface`,
`search_queries.tier`, `ix_search_queries_at`, `ix_llm_calls_at` and
`ix_llm_calls_generation_id`.

**Two things are asserted off the catalog rather than off `Base.metadata`**,
because `compare_metadata` is blind to both: a partial index's predicate (a
full index answers every membership check a partial one does), and a
`server_default` (the model would report whatever the model says, which is not
what the migration left behind).

**And two things need a database the session fixture cannot give**, so they
build a scratch one and drive alembic through it: the backfill, which is only
observable against a row that existed *before* the migration ran, and the
planner probe, which needs the pre-`m10c` schema as its control arm.
"""

import asyncio
import functools
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    column_set,
    drop_database,
    index_set,
    run_alembic,
    scratch_database,
)
from usher.db.base import build_engine
from usher.domain.ids import new_id

_RETENTION_DELETE = "DELETE FROM search_queries WHERE at < now() - interval '90 days'"
"""PRD 10's own pruning statement, verbatim
(`docs/prd/10-telemetry-and-dashboards.md`, `## Analytics tables`). Quoted
rather than paraphrased: an index that serves a statement nobody writes is
`ix_titles_popularity` again."""


async def _indexdef(url: str, name: str) -> str:
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            definition = (
                await conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                    {"name": name},
                )
            ).scalar_one()
            return str(definition)
    finally:
        await engine.dispose()


# --- the five artefacts, one case each ------------------------------------


async def test_search_queries_has_the_surface_column(postgres_url: str) -> None:
    assert "surface" in await column_set(postgres_url, "search_queries")


async def test_search_queries_has_the_tier_column(postgres_url: str) -> None:
    assert "tier" in await column_set(postgres_url, "search_queries")


async def test_the_retention_index_exists(postgres_url: str) -> None:
    assert "ix_search_queries_at" in await index_set(postgres_url)


async def test_the_cost_ledgers_time_index_exists(postgres_url: str) -> None:
    assert "ix_llm_calls_at" in await index_set(postgres_url)


async def test_the_cost_ledgers_generation_index_exists(postgres_url: str) -> None:
    assert "ix_llm_calls_generation_id" in await index_set(postgres_url)


# --- the shapes `compare_metadata` cannot see -----------------------------


async def test_the_generation_index_is_partial_and_says_so_in_its_own_definition(
    postgres_url: str,
) -> None:
    """Asserted **as text in `indexdef`**, because `compare_metadata` is blind
    to a partial index's predicate and a full index answers every membership
    check a partial one does -- so the case above this one cannot tell them
    apart, by construction.

    `m08a`'s docstring is where the predicate comes from: query-expansion rows
    carry `NULL` and are the majority of the table once Task 20 ships, and they
    are exactly the rows the `curated_rows` join never wants.
    """
    definition = await _indexdef(postgres_url, "ix_llm_calls_generation_id")
    assert "WHERE (generation_id IS NOT NULL)" in definition, definition


async def test_the_two_new_columns_carry_no_server_default(postgres_url: str) -> None:
    """Read off `information_schema.columns.column_default`, **not off the
    model**, and the distinction is the whole case: `m09d`'s rule is that a
    `server_default` *"would outlive this migration and supply a plausible
    wrong value to a writer that forgot"*, so what has to be checked is what
    the migration left in the catalog rather than what the mapped class says.

    `surface` also carries its `NOT NULL`, asserted here rather than in a
    fourth case because a nullable column with no default is the state
    `upgrade()`'s *middle* statement leaves and is exactly what dropping the
    `SET NOT NULL` produces.
    """
    engine = build_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name, column_default, is_nullable, "
                        "       data_type, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'search_queries' "
                        "  AND column_name IN ('surface', 'tier')"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    shapes = {row[0]: tuple(row[1:]) for row in rows}
    assert shapes == {
        "surface": (None, "NO", "character varying", 8),
        "tier": (None, "YES", "character varying", 6),
    }


async def test_a_search_row_with_no_tier_round_trips(session: AsyncSession) -> None:
    """`tier` is nullable **by design and not by oversight**, so the design
    gets a case: a `surface='search'` row carrying `tier IS NULL` is the
    ordinary shape of every row in this table today, and a `NOT NULL` added by
    a later hand would need a sentinel member meaning "not applicable" in the
    one column whose purpose is to keep two vocabularies apart.
    """
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name, is_default) VALUES (:id, :n, false)"),
        {"id": user_id, "n": f"tierless-{uuid.uuid4().hex[:8]}"},
    )
    row_id = new_id()
    await session.execute(
        text(
            "INSERT INTO search_queries "
            "(id, at, user_id, query, mode, result_count, latency_ms, "
            " clicked_title_id, played, surface, tier) "
            "VALUES (:id, :at, :user_id, 'the quiet vacuum', 'fused', 3, 12, "
            "        NULL, false, 'search', NULL)"
        ),
        {"id": row_id, "at": datetime.now(UTC), "user_id": user_id},
    )
    read = (
        await session.execute(
            text("SELECT surface, tier FROM search_queries WHERE id = :id"), {"id": row_id}
        )
    ).one()
    assert read == ("search", None)


# --- the two cases that need a database of their own ----------------------


async def test_the_backfill_reaches_a_row_that_existed_before_the_migration_ran(
    postgres_url: str,
) -> None:
    """**An empty-table upgrade satisfies a three-statement backfill exactly as
    well as a correct one**, so the row is what makes the `UPDATE` observable
    at all. Seeded against the `m10b` schema, read back above `m10c`.

    The deployment's own nine rows -- 107 of them as of 2026-08-26 -- are not a
    test population: asserting against whatever the developer's database
    happens to hold is *"a run that did not run is not a pass"* wearing a
    `SELECT`.

    `'search'` is asserted as the **true** value rather than a filled one:
    every row this table holds came from `GET /search` or `usher search`,
    because `SearchService._record_search` is reachable from exactly those two
    callers and `SearchService.suggest` writes nothing at this revision.
    """
    admin, scratch, url = await scratch_database(postgres_url, "backfill")
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10b", direction="up"))
        engine = build_engine(url)
        user_id = new_id()
        row_id = new_id()
        try:
            async with engine.begin() as conn:
                # The premise, asserted rather than assumed: the column this
                # case is about does not exist yet, so the seeded row really
                # is a *pre-existing* row and not one the migration wrote.
                below = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'search_queries'"
                        )
                    )
                ).scalars()
                assert "surface" not in set(below), "the premise: `surface` is `m10c`'s to add"
                await conn.execute(
                    text(
                        "INSERT INTO users (id, name, is_default) VALUES (:id, 'backfilled', false)"
                    ),
                    {"id": user_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO search_queries "
                        "(id, at, user_id, query, mode, result_count, latency_ms, "
                        " clicked_title_id, played) "
                        "VALUES (:id, :at, :user_id, 'a query from before', 'full_text', "
                        "        1, 4, NULL, false)"
                    ),
                    {"id": row_id, "at": datetime.now(UTC), "user_id": user_id},
                )

            await asyncio.to_thread(run_alembic, url, "head")

            async with engine.connect() as conn:
                above = (
                    await conn.execute(
                        text("SELECT surface, tier FROM search_queries WHERE id = :id"),
                        {"id": row_id},
                    )
                ).one()
            assert above == ("search", None)
        finally:
            await engine.dispose()
    finally:
        await drop_database(admin, scratch)


async def _delete_plan(url: str) -> str:
    """`EXPLAIN` of PRD 10's pruning `DELETE`, under `enable_seqscan = off`.

    `SET LOCAL` inside a transaction that is rolled back, and the `DELETE` is
    never executed -- `EXPLAIN` without `ANALYZE` plans the statement and
    discards it.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            rows = (await conn.execute(text(f"EXPLAIN {_RETENTION_DELETE}"))).scalars()
            return "\n".join(rows)
    finally:
        await engine.dispose()


async def test_the_retention_delete_plans_onto_the_index_and_did_not_before(
    postgres_url: str,
) -> None:
    """**An index that exists proves nothing about what it serves.** This is
    the discipline `test_both_new_foreign_keys_have_an_index_the_referential_check_can_use`
    already applies, one table over.

    Two arms on one scratch database, and the pre-migration arm is what makes
    the post-migration arm a measurement rather than a hope: with
    `enable_seqscan = off` Postgres falls back to a `Seq Scan` when there is no
    alternative -- at the disabled-node penalty, but it still plans it -- so at
    `m10b` this statement reads `Seq Scan` and at `m10c` it reads
    `ix_search_queries_at`. The two arms differ observably, which is the
    premise proved rather than assumed.
    """
    admin, scratch, url = await scratch_database(postgres_url, "prune")
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10b", direction="up"))
        before = await _delete_plan(url)
        assert "Seq Scan on search_queries" in before, before
        assert "ix_search_queries_at" not in before, before

        await asyncio.to_thread(run_alembic, url, "head")
        after = await _delete_plan(url)
        assert "ix_search_queries_at" in after, after
    finally:
        await drop_database(admin, scratch)


@pytest.mark.parametrize(
    "artefact",
    [
        "surface",
        "tier",
        "ix_search_queries_at",
        "ix_llm_calls_at",
        "ix_llm_calls_generation_id",
    ],
)
async def test_one_step_back_and_forward_restores_each_artefact(
    postgres_url: str, artefact: str
) -> None:
    """Down to `m10b` then back up, parametrised so a
    `downgrade()`/`upgrade()` pair that forgets one of the five fails naming
    *that* one rather than the first.

    **A named stop rather than `-1`**, which is what this read while `m10c` was
    head: `-1` follows the chain, so the moment a later revision lands it
    exercises that one's `downgrade()` and every assertion here becomes a
    statement about a schema `m10c` never touched.

    `run_alembic` is called with an explicit `direction=` for the bare revision
    id: left to infer, a bare id runs `upgrade`, which against a database
    already past it is a silent no-op and the assertions then describe a schema
    nobody moved. The stop is `m10c` rather than `head` so that `-1` keeps
    meaning "below the revision these five artefacts belong to".
    """
    admin, scratch, url = await scratch_database(postgres_url, "cycle1")
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10c", direction="up"))

        async def present() -> bool:
            if artefact in ("surface", "tier"):
                return artefact in await column_set(url, "search_queries")
            return artefact in await index_set(url)

        assert await present(), f"the premise: {artefact} exists at head"
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10b", direction="down"))
        assert not await present(), f"{artefact} outlived `m10c.downgrade()`"
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10c", direction="up"))
        assert await present(), f"{artefact} did not come back"
    finally:
        await drop_database(admin, scratch)


async def test_a_down_and_up_cycle_relabels_a_suggest_row_and_the_artefact_check_cannot_see_it(
    postgres_url: str,
) -> None:
    """🔴 **The five artefacts above come back and the data does not**, and
    nothing in this file could say so: every assertion beside this one reads
    `information_schema` or `pg_indexes`, so a cycle that restored the whole
    schema over silently rewritten rows passes all five.

    `m10c.downgrade()` drops `surface` and `tier` from a table it does not
    drop, so the values are gone with no side table to park them in; the
    re-`upgrade()` then backfills `'search'` over every row, which is *true*
    of every row that existed at `m10c` and **false of every row J2's writer
    has written since**. The migration's docstring states this and this case
    is what makes the statement a measurement -- a paragraph nothing runs is
    how a claim about reversibility goes stale.

    ⚠️ **It is deliberately not a test of the missing `WHERE`.**
    `WHERE surface IS NULL` on that `UPDATE` would change nothing here,
    because the column has just been re-added and every row is NULL; the
    assertions below would read identically with it in place. What is being
    pinned is the `drop_column`, which is where the values actually go.

    The `search` row is the control. Both rows go round the same cycle, and
    only one of them comes back carrying a different fact -- without it,
    *"the suggest row reads `search` afterwards"* is also what a cycle that
    deleted every row and re-seeded defaults would produce.
    """
    admin, scratch, url = await scratch_database(postgres_url, "relabel")
    try:
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10c", direction="up"))
        engine = build_engine(url)
        user_id, suggest_id, search_id = new_id(), new_id(), new_id()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO users (id, name, is_default) VALUES (:id, :n, false)"),
                    {"id": user_id, "n": f"relabelled-{uuid.uuid4().hex[:8]}"},
                )
                for row_id, surface, tier in (
                    (suggest_id, "suggest", "prefix"),
                    (search_id, "search", None),
                ):
                    await conn.execute(
                        text(
                            "INSERT INTO search_queries "
                            "(id, at, user_id, query, mode, result_count, latency_ms, "
                            " clicked_title_id, played, surface, tier) "
                            "VALUES (:id, :at, :user_id, 'the quie', 'full_text', 3, 4, "
                            "        NULL, false, :surface, :tier)"
                        ),
                        {
                            "id": row_id,
                            "at": datetime.now(UTC),
                            "user_id": user_id,
                            "surface": surface,
                            "tier": tier,
                        },
                    )

            async def read(row_id: uuid.UUID) -> tuple[str, str | None] | None:
                async with engine.connect() as conn:
                    found = (
                        await conn.execute(
                            text("SELECT surface, tier FROM search_queries WHERE id = :id"),
                            {"id": row_id},
                        )
                    ).one_or_none()
                return None if found is None else (found[0], found[1])

            # The premise. Without it the assertion after the cycle is about
            # nothing: a row that never carried `('suggest', 'prefix')` reads
            # back `('search', None)` for a reason that is not the migration's.
            assert await read(suggest_id) == ("suggest", "prefix")
            assert await read(search_id) == ("search", None)

            # `m10b` by name rather than `-1`: this cycle is about `m10c`'s
            # two columns, and `-1` would exercise whichever revision is head.
            await asyncio.to_thread(functools.partial(run_alembic, url, "m10b", direction="down"))
            await asyncio.to_thread(functools.partial(run_alembic, url, "m10c", direction="up"))

            # The row survives -- this is a relabelling, not a deletion, which
            # is what makes it silent. A dropped row would at least be a
            # missing count somewhere.
            assert await read(suggest_id) == ("search", None), (
                "the cycle is data-destructive and the migration docstring says so; "
                "if this now holds `('suggest', 'prefix')` something restored the "
                "values and that docstring is the thing to correct"
            )
            assert await read(search_id) == ("search", None), (
                "the control: a `search` row round-trips unchanged, so the "
                "assertion above is about the surface and not about the cycle"
            )
        finally:
            await engine.dispose()
    finally:
        await drop_database(admin, scratch)
