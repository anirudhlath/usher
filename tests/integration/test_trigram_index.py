"""The trigram index the type-ahead path scans, and the GUC that bounds it.

Every assertion here is about the *plan* or about the *candidate set*, never
about wall clock. A latency assertion on a near-empty test table measures
the fixture, and this suite has a standing rule against a check that any
implementation satisfies.
"""

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
    """Verified rather than assumed, because the schema right next door does
    the opposite for a different reason.

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
    """The wrong implementations this fails: no index at all, and a GIN index
    built without `gin_trgm_ops` (which is not an error -- it just silently
    cannot serve `%`).

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
    """The pooled-connection hazard, pinned.

    A bare `SET pg_trgm.similarity_threshold = 0.2` outlives the transaction,
    outlives the request, and hands the *next* unrelated caller on that
    pooled connection a threshold it never chose -- which changes how many
    rows a search returns, silently, for a reason nothing in a log can
    explain. `SET LOCAL` reverts at the transaction boundary.

    The boundary used here is a SAVEPOINT rather than the whole transaction,
    and that is a deliberate departure from the plan's own draft. This
    suite's `session` fixture *is* one transaction, rolled back in teardown,
    so a `session.rollback()` inside a case ends the fixture's transaction
    under it. PostgreSQL reverts a `SET LOCAL` on `ROLLBACK TO SAVEPOINT`
    exactly as it does on `ROLLBACK`, so the property under test is the same
    one and the fixture survives.

    **This case does not distinguish `SET` from `SET LOCAL`, and saying so is
    the point.** PostgreSQL reverts a *bare* `SET` too when the transaction
    that issued it is rolled back, so any rollback-based case -- including the
    plan's own draft, which used `session.rollback()` -- passes against both
    spellings. Measured: mutating this case's `SET LOCAL` to `SET` leaves it
    green. What it does pin is that the threshold is transaction-scoped and
    the default is 0.3.
    `test_a_bare_set_outlives_a_commit_and_set_local_does_not` is the one with
    teeth, because the difference only appears at COMMIT.
    """
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
    """The pooled-connection hazard itself, on the only boundary that shows
    it.

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
    """The lazy-load trap, measured for `pg_trgm` rather than worked around
    silently in `_warm`, and it is sharper than the `hnsw.%` version the plan
    records.

    Four measured facts, in the order they bite:

    1. On a backend that has done nothing `pg_trgm`-related,
       `SHOW pg_trgm.similarity_threshold` **raises**
       `unrecognized configuration parameter`. The GUC is registered by the
       library's `_PG_init`, and the library is loaded lazily per backend.
    2. A *failed* `SHOW` does not load it. Retrying gets the same error, so
       there is no self-healing probe here.
    3. `SET LOCAL` of that same GUC **does** load it, and succeeds, and
       `SHOW` works from then on. So the write path never sees the failure
       the read path does.
    4. `pg_settings` still reports **zero** `pg_trgm%` rows at that point,
       while `SHOW` returns the value that was just set. The two disagree.

    Together those make any feature-detection of a contrib GUC a
    flaky-test generator and a worse production check: the answer is decided
    by whatever ran earlier on the pooled connection this caller happened to
    be handed. `PostgresSuggestIndex` must therefore `SET LOCAL` and move on,
    never probe first.
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
    """The cliff, asserted as a candidate set rather than quoted as a number,
    so it stays true on whatever data the fixture holds.

    Measured at 300k rows against one prefix: 0.1 -> 8,020 candidates,
    0.2 -> 5,611, 0.3 -> 1,774, 0.4 -> 1,057, **0.5 -> 23**. Between 0.4 and
    0.5 the set collapses 46x, so above roughly 0.45 a single-character typo
    on a short title produces no candidate at all -- which is exactly the
    weakness ADR-0002 names.

    This is why 0.3 stays. The wrong implementation it fails: a
    `PostgresSuggestIndex` that "tightens" the threshold to reduce the
    re-rank's work, trading away the one thing the fuzzy path exists for.

    **The pair is chosen by measurement, not by eye, and the plan's own draft
    pair does not work.** `similarity('Harbour Nine', 'harbor nine')` is
    0.6667 -- still a match at 0.6, so that case asserted `0 == 1`. Length is
    what decides it: a one-character typo on a long name barely moves the
    trigram overlap. `similarity('Iron', 'irom')` is **0.4286**, which lands
    inside the 0.4-to-0.5 band where the measured candidate set collapses
    46x, so this pair straddles the cliff instead of sitting well clear of
    it -- and a short title with a single-character typo is precisely the
    weakness ADR-0002 names.
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
