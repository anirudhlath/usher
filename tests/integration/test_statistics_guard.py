"""The machinery `conftest.py` added for issue #79, against a real Postgres.

Every other file in this directory *relies* on `session`'s teardown asserting
that `pg_class` describes this database. A guard nothing plants against passes
exactly like a guard that works — the standing rule this repository has learned
from a `sitecustomize.py` that was never on `PYTHONPATH` and an import contract
that substituted an anchor string that did not exist — so the leak is created
here on purpose and the guard is watched finding it.

These cases are also the executable form of two findings that are otherwise
only prose: `ANALYZE` outliving its own rollback (issues #26, #43) and
`CREATE INDEX` doing the identical thing through the identical catalog path,
which no grep for `ANALYZE` would ever have turned up (#79).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from tests.integration.conftest import (
    _restore_the_statistics,
    _tables_pg_class_is_wrong_about,
    index_suspended,
    total_cost,
)
from usher.db.base import build_engine

_SEEDED = 500

_SEED = (
    "INSERT INTO titles (id, kind, name, sort_name, enrichment_state) "
    "SELECT gen_random_uuid(), 'movie', 'Guarded ' || i, 'Guarded ' || i, 'skeleton' "
    "FROM generate_series(1, :rows) AS i"
)

_NOTHING_FORGIVEN = frozenset[str]()


async def _committed_titles(conn: AsyncConnection) -> int:
    """What `titles` really holds, which is not always zero: a route-driven
    test commits for real, so this file cannot assume an empty catalog and
    asserts on the *difference* the seed makes instead."""
    return int((await conn.execute(text("SELECT count(*) FROM titles"))).scalar_one())


@pytest.mark.parametrize(
    ("what", "leak"),
    [
        ("ANALYZE", ["ANALYZE titles"]),
        # The one a grep for `ANALYZE` cannot find. `CREATE INDEX` scans the
        # heap to build the index and then writes the *heap's* `reltuples` and
        # `relpages` through the same in-place path, so a rolled-back rebuild
        # leaves the same lie. This is how `bulk_load_window`'s cases leak.
        (
            "CREATE INDEX",
            [
                "DROP INDEX ix_titles_sort_name",
                "CREATE INDEX ix_titles_sort_name ON titles (sort_name)",
            ],
        ),
    ],
)
async def test_the_guard_catches_statistics_that_outlived_their_rollback(
    postgres_url: str, what: str, leak: list[str]
) -> None:
    """The plant, on its own connection so the leak is real rather than
    arranged.

    It has to be a connection of this case's own: the lie only exists *after*
    a rollback, and the `session` fixture's rollback happens in its teardown,
    which is where the guard being tested already runs. So the whole
    seed/leak/roll-back/observe cycle is driven here, exactly as
    `restores_the_statistics_this_seed_leaks` drives its `VACUUM`.
    """
    engine = build_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            already = await _committed_titles(conn)
            await conn.execute(text(_SEED), {"rows": _SEEDED})
            for statement in leak:
                await conn.execute(text(statement))
            await conn.rollback()

            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            lying = await _tables_pg_class_is_wrong_about(autocommit, _NOTHING_FORGIVEN)
            assert lying.get("titles") == (already + _SEEDED, already), (
                f"{what} did not leave `pg_class` describing the rows it rolled back, so "
                f"this case is planting nothing: {lying}"
            )

            await _restore_the_statistics(autocommit, frozenset({"titles"}))
            assert "titles" not in await _tables_pg_class_is_wrong_about(
                autocommit, _NOTHING_FORGIVEN
            ), "the repair the guard runs did not put `pg_class` back"
    finally:
        await engine.dispose()


async def test_the_guard_is_quiet_when_pg_class_is_telling_the_truth(
    postgres_url: str,
) -> None:
    """The control for the case above, and the reason it is evidence.

    A check that reported *every* table would 'catch' the plant while catching
    a clean database too. Seeding without leaking is the same fixture minus the
    one statement under test.
    """
    engine = build_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text(_SEED), {"rows": _SEEDED})
            await conn.rollback()
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            assert "titles" not in await _tables_pg_class_is_wrong_about(
                autocommit, _NOTHING_FORGIVEN
            )
    finally:
        await engine.dispose()


async def test_a_suspended_index_leaves_the_plan_and_comes_back(session: AsyncSession) -> None:
    """`index_suspended` is only worth having if the planner really stops
    seeing the index and really starts again.

    Both halves are asserted, because a context manager whose `UPDATE` silently
    matched nothing would leave every margin assertion in this suite comparing
    a plan against itself — passing while measuring nothing, which is the
    failure mode #79 exists to remove rather than to introduce somewhere new.
    """
    reads_valid = text("SELECT indisvalid FROM pg_index WHERE indexrelid = CAST(:name AS regclass)")
    probe = text("EXPLAIN SELECT id FROM titles ORDER BY sort_name LIMIT 5")
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    assert (await session.execute(reads_valid, {"name": "ix_titles_sort_name"})).scalar_one()
    before = "\n".join(row[0] for row in await session.execute(probe))
    assert "ix_titles_sort_name" in before, before

    async with index_suspended(session, "ix_titles_sort_name"):
        assert not (
            await session.execute(reads_valid, {"name": "ix_titles_sort_name"})
        ).scalar_one()
        during = "\n".join(row[0] for row in await session.execute(probe))
        assert "ix_titles_sort_name" not in during, during

    assert (await session.execute(reads_valid, {"name": "ix_titles_sort_name"})).scalar_one()
    after = "\n".join(row[0] for row in await session.execute(probe))
    assert "ix_titles_sort_name" in after, after


def test_total_cost_reads_the_root_nodes_total_and_not_its_start() -> None:
    """The `start..total` distinction is the whole point: two plans routinely
    tie on the cost of returning the *first* row while differing by orders of
    magnitude over the whole result, so a margin taken off `start` would
    compare the numbers that agree."""
    assert (
        total_cost(
            "Update on media_items  (cost=4.94..31.72 rows=0 width=0)\n"
            "  ->  Bitmap Heap Scan on media_items  (cost=4.94..31.72 rows=52 width=7)"
        )
        == 31.72
    )


def test_total_cost_refuses_a_plan_that_was_explained_without_costs() -> None:
    """`EXPLAIN (COSTS OFF)` returns a perfectly good plan with no numbers in
    it, and a helper that answered 0.0 there would make every margin assertion
    below it pass trivially."""
    with pytest.raises(AssertionError, match="did not run with costs"):
        total_cost("Update on media_items\n  ->  Bitmap Heap Scan on media_items")
