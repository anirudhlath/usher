"""The trigram index the type-ahead path scans, and the GUC that bounds it."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import build_engine
from usher.domain.ids import new_id


async def _seed(session: AsyncSession, names: list[str]) -> None:
    for name in names:
        await session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :name, :name)"
            ),
            {"id": new_id(), "name": name},
        )


async def _show_trgm(session: AsyncSession, value: str) -> str:
    result = await session.execute(text("SELECT CAST(show_trgm(:v) AS text)"), {"v": value})
    return str(result.scalar_one())


async def _warm(session: AsyncSession) -> None:
    """Load `pg_trgm` into this backend before anything reads its GUC.

    **Measured, and it is the same trap the plan records for `hnsw.%`.** A
    contrib library's GUCs do not exist on a backend that has not yet
    executed one of its operators, so on a *cold* connection
    `SHOW pg_trgm.similarity_threshold` raises
    `UndefinedObjectError: unrecognized configuration parameter`, while
    `SET LOCAL pg_trgm.similarity_threshold = ...` on that same cold
    connection **succeeds**. That asymmetry is a flaky-test generator and a
    worse feature-detector: a probe reports "absent" when cold and "present"
    when warm, on identical code, decided by whatever ran earlier on the
    pooled connection this session happened to check out.

    Never feature-detect a contrib GUC via `SHOW`, `pg_settings` or
    `current_setting`. Warm it, or set it and move on. The full four-part
    measurement is pinned by
    `test_a_contrib_guc_is_unreadable_until_something_loads_the_library`.
    """
    await session.execute(text("SELECT similarity('a', 'b')"))


async def _threshold(session: AsyncSession) -> float:
    result = await session.execute(text("SHOW pg_trgm.similarity_threshold"))
    return float(result.scalar_one())


async def test_pg_trgm_folds_case_so_the_index_is_on_the_raw_column(
    session: AsyncSession,
) -> None:
    """Verified rather than assumed.

    because the schema right next door does the opposite for a different reason.

    `ix_titles_name_lower_year` is an expression index on `lower(name)`,
    because a *btree* equality lookup is case-sensitive and the matcher
    normalises. Trigram matching is not: `pg_trgm` lowercases while
    generating trigrams, so `%` and `similarity()` are already
    case-insensitive and an index on the raw column serves them.

    If this ever fails, the fix is an expression index on `lower(name)` plus
    a query to match -- not a `lower()` wrapped around the bind parameter,
    which would silently stop using the index while still returning answers.
    """
    assert await _show_trgm(session, "Harbour Nine") == await _show_trgm(session, "harbour nine")


async def test_a_fuzzy_lookup_uses_the_trigram_index(session: AsyncSession) -> None:
    """The wrong implementations this fails.

    no index at all, and a GIN index built without `gin_trgm_ops` (which is not an error
    -- it just silently cannot serve `%`).

    `enable_seqscan = off` is what makes the claim observable on a small
    fixture. A near-empty table seq-scans regardless of how many indexes it
    has, so without this the case passes against a schema with none.
    """
    await _seed(session, ["The Quiet Vacuum", "Harbour Nine", "Autumn Iron"])
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(text("EXPLAIN SELECT id FROM titles WHERE name % 'quiet vacum'"))
    plan = "\n".join(row[0] for row in result)
    assert "ix_titles_name_trgm" in plan, plan


async def test_the_similarity_threshold_is_set_local_and_does_not_outlive_it(
    session: AsyncSession,
) -> None:
    """The pooled-connection hazard, pinned."""
    await _warm(session)
    assert await _threshold(session) == pytest.approx(0.3)

    savepoint = await session.begin_nested()
    await session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.45"))
    assert await _threshold(session) == pytest.approx(0.45)
    await savepoint.rollback()

    assert await _threshold(session) == pytest.approx(0.3)


async def test_a_bare_set_outlives_a_commit_and_set_local_does_not(
    postgres_url: str,
) -> None:
    """The pooled-connection hazard itself, on the only boundary that shows it.

    `SET LOCAL` ends with the transaction. A bare `SET` ends with the
    *session* -- so on a committed transaction it survives, and the pooled
    connection carries the caller's threshold to whoever checks it out next.
    That changes how many rows an unrelated search returns, silently, for a
    reason nothing in a log can explain.

    Needs its own engine rather than the suite's `session` fixture, because
    the discriminating boundary is a COMMIT and that fixture is one
    transaction rolled back in teardown. The engine is disposed in a
    `finally`, so the leaked GUC goes with the connection that held it.

    The wrong implementation this fails: `SET` in place of `SET LOCAL` in
    `PostgresSuggestIndex` (Task 17).
    """
    engine = build_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT similarity('a', 'b')"))  # load the library

            await conn.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.45"))
            await conn.commit()
            reverted = await conn.execute(text("SHOW pg_trgm.similarity_threshold"))
            assert float(reverted.scalar_one()) == pytest.approx(0.3)

            await conn.execute(text("SET pg_trgm.similarity_threshold = 0.45"))
            await conn.commit()
            leaked = await conn.execute(text("SHOW pg_trgm.similarity_threshold"))
            assert float(leaked.scalar_one()) == pytest.approx(0.45)
    finally:
        await engine.dispose()


async def test_a_contrib_guc_is_unreadable_until_something_loads_the_library(
    session: AsyncSession,
) -> None:
    """The lazy-load trap.

    measured for `pg_trgm` rather than worked around silently in `_warm`, and it is
    sharper than the `hnsw.%` version the plan records.
    """
    savepoint = await session.begin_nested()
    with pytest.raises(ProgrammingError):
        await session.execute(text("SHOW pg_trgm.similarity_threshold"))
    await savepoint.rollback()

    retry = await session.begin_nested()
    with pytest.raises(ProgrammingError):
        await session.execute(text("SHOW pg_trgm.similarity_threshold"))
    await retry.rollback()

    await session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.42"))
    assert await _threshold(session) == pytest.approx(0.42)

    catalogued = await session.execute(
        text("SELECT count(*) FROM pg_settings WHERE name LIKE 'pg_trgm%'")
    )
    assert catalogued.scalar_one() == 0, "pg_settings and SHOW no longer disagree"


async def test_a_high_threshold_destroys_fuzzy_recall(session: AsyncSession) -> None:
    """The cliff.

    asserted as a candidate set rather than quoted as a number, so it stays true on
    whatever data the fixture holds.
    """
    await _seed(session, ["Iron"])
    typo = "irom"

    await session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.3"))
    at_default = await session.execute(
        text("SELECT count(*) FROM titles WHERE name % :q"), {"q": typo}
    )
    await session.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.5"))
    at_high = await session.execute(
        text("SELECT count(*) FROM titles WHERE name % :q"), {"q": typo}
    )

    assert at_default.scalar_one() == 1
    assert at_high.scalar_one() == 0
