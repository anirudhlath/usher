"""TMDb's <=6-month caching term, which is the one dashboard panel in PRD 10
whose failure is a **licence breach** rather than a blind spot.

`04-catalog-bootstrap.md`'s hard rules encode two halves of the third-party
data posture. `tests/unit/test_no_third_party_data.py` enforces the
*redistribution* half mechanically -- nothing ships a prebuilt database, nothing
scrapes imdb.com. **Nothing enforced the retention half**, and this module plus
dashboard 5's threshold line is the whole of it.

The series is `raw_payloads.fetched_at`, not `titles.enriched_at`, and the
difference is the obligation rather than a detail: `enriched_at` records when
*Usher* enriched a title, `fetched_at` records when the *provider's response*
was cached, and they diverge exactly when a title is enriched from an
already-cached payload -- the case the ceiling exists for.
[ADR-0016](../../docs/prd/decisions/0016-raw-payloads-cache-providers-not-sources.md)
settled this by refusing a `provider_cache_meta` table by name; the column and
`ix_raw_payloads_fetched_at` that replaced it ship in
`e5b8f2c40d17_ingest_pipeline` (M4).

**The SQL is not written here.** It is extracted from PRD 10 dashboard 5's own
```sql fence by `compliance_panel_sql`, so what this suite proves is what the
PRD specifies and what D10's dashboard copies. A transcription would be a
third place to keep in step, and the one that drifts silently is always the
one nobody executes.

**Two claims, two fixtures**, which is the shape `mutation-sweeps.md` records
for D4's TTL and B9's cast limit:

- The **count** arm runs at six rows, where `>` versus `>=` is decided by a
  single row sitting exactly on `now() - interval '6 months'` and the answer is
  deterministic. At six rows Postgres seq-scans and is right to, so this arm
  asserts nothing about plans.
- The **plan** arm seeds `_PLAN_ROWS` and prices the runner-up with
  `index_suspended`, because "the planner chose the index I meant" is a
  tie-break rather than a property of the schema until the alternative is
  measurably worse (#79).

⚠️ **`now()` is `transaction_timestamp()` and is frozen for the life of the
transaction**, which is what makes the boundary row exact here: the seed and the
panel's own predicate compute the same instant, so the row lands on the ceiling
rather than a few microseconds either side of it. The store's `put` deliberately
uses `clock_timestamp()` for the opposite reason
(`test_raw_payload_store.py::test_a_refresh_moves_fetched_at_inside_one_transaction`),
so these rows are inserted directly rather than through the port.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import A_DECISIVE_MARGIN, Analyze, index_suspended, total_cost
from tests.unit.test_prd_10_panel_backing import compliance_panel_sql
from usher.domain.ids import new_id

_SEED = text(
    "INSERT INTO raw_payloads (id, provider, kind, reference, payload, fetched_at) "
    "VALUES (:id, 'tmdb', 'movie', :reference, '{}'::jsonb, "
    "now() - interval '6 months' + (:offset_days * interval '1 day'))"
)
"""One payload, placed relative to the ceiling rather than to `now()`, so the
fixture reads as its own diagram and the boundary case is the literal `0`.

The offset is a *count* multiplied by `interval '1 day'` rather than an interval
literal bound as a parameter: asyncpg infers `$n`'s type from the cast around it,
so `CAST(:offset AS interval)` makes it reject the Python `str` it was handed
before the statement ever reaches Postgres.
"""

_IN_TERM = (1, 30, 100)
_PAST_CEILING = (-1, -30)
_ON_THE_CEILING = 0

_PLAN_ROWS = 5_000
"""Enough that the planner prefers the index over a scan, and small enough to
seed in one `INSERT ... SELECT`. The count arm deliberately does **not** run at
this size: `A_DECISIVE_MARGIN`'s docstring records what a plan assertion costs
at fixture scale, and a count assertion pays none of it."""


def _executable(statement: str) -> str:
    """The statement without its leading `--` commentary.

    PRD 10 labels each target in the fence, and `EXPLAIN <statement>` would put
    that label between the keyword and the query -- legal, but it reads as a
    trap the first time it is edited. Stripped for both arms, so the two run the
    same text.
    """
    lines = [line for line in statement.splitlines() if not line.strip().startswith("--")]
    return "\n".join(lines).strip()


async def _explain(session: AsyncSession, statement: str) -> str:
    """`EXPLAIN` in **text** format, because `total_cost` reads the root node's
    cost off the first line."""
    rows = (await session.execute(text(f"EXPLAIN {_executable(statement)}"))).scalars().all()
    return "\n".join(rows)


async def test_the_cache_age_panel_counts_the_rows_past_the_ceiling(
    session: AsyncSession,
) -> None:
    """**The boundary row is not past the ceiling**, which is what makes `<`
    versus `<=` a decision rather than a detail: TMDb's term is *<=6 months*, so
    a payload cached exactly six months ago is still in term and a panel that
    counts it is reporting a breach that has not happened.

    The sweep target this arm exists for is the ceiling respelled as
    `interval '180 days'`. Measured on `pgvector/pgvector:pg17` over the 1,461
    days from 2026-01-01 to 2029-12-31, `now() - interval '180 days'` is **1 to
    4 days later** than `now() - interval '6 months'` and is never equal to it,
    so the boundary row is counted under the respelling and this case is red
    every day of the year rather than seasonally.

    Note the direction, because M10's task text has it backwards: the later
    cutoff matches *more* rows, so `'180 days'` over-reports the breach. It is
    wrong because it is not the term TMDb states, not because it flatters.
    """
    statements = compliance_panel_sql()
    for offset in (*_IN_TERM, *_PAST_CEILING, _ON_THE_CEILING):
        await session.execute(
            _SEED,
            {"id": new_id(), "reference": f"{offset:+d}d", "offset_days": offset},
        )

    oldest, ceiling = (await session.execute(text(_executable(statements[0])))).one()
    past_ceiling = (await session.execute(text(_executable(statements[1])))).scalar_one()
    cached, share = (await session.execute(text(_executable(statements[2])))).one()

    # The positive control, before the value it defends: a fixture that seeded
    # nothing answers `0 == 0` to every question below.
    assert cached == 6, f"the six payloads are not in the table, {cached} are"
    assert oldest < ceiling, "no seeded payload is past the ceiling, so the count cannot be"
    assert cached - past_ceiling >= 1, "no seeded payload is in term, so nothing is being spared"
    on_the_ceiling = (
        await session.execute(
            text(
                "SELECT count(*) FROM raw_payloads WHERE provider = 'tmdb' "
                "AND fetched_at = now() - interval '6 months'"
            )
        )
    ).scalar_one()
    assert on_the_ceiling == 1, (
        "no payload sits exactly on the ceiling, so this case cannot tell < from <= and "
        "the count below is satisfied by either"
    )

    assert past_ceiling == 2, (
        f"expected the two payloads older than six months and not the one exactly on the "
        f"ceiling, got {past_ceiling}. Measured: `<=` for `<` reads 3 (the boundary row "
        f"joins them) and `interval '180 days'` for `interval '6 months'` reads 4 on this "
        f"date (the ceiling moves 1-4 days later, taking the in-term row beside it too)"
    )
    assert float(share) == pytest.approx(2 / 6), (
        f"the share is the count over count(*), not over the rows past the ceiling: {share}"
    )


async def test_the_cache_age_panel_plans_onto_the_fetched_at_index(
    session: AsyncSession, analyze: Analyze
) -> None:
    """`ix_raw_payloads_fetched_at` is ascending **because the question asks for
    the minimum**, and this is what proves it is asked that way.

    **Two of the panel's three targets are index-served and the third cannot
    be**, which is why PRD 10 specifies three statements rather than one.
    `count(*)` over the whole cache has to read every row, so folding it in with
    the other two costs both of them the index -- measured on the live catalog
    2026-09-07 at 133,501 payloads: separately the three plan at 0.48, 4.46 and
    4,813; combined, one `Parallel Seq Scan` with the index unused. The third
    arm below pins that as a *design* statement rather than leaving it as an
    unexplained seq scan somebody later "fixes" with an index that cannot help.

    Measured on `pgvector/pgvector:pg17` 2026-09-07 at `_PLAN_ROWS`, by
    suspending the index rather than inferring from the winner's cost: the
    oldest entry is **0.35** against **122.02** (349x) and the count **4.32**
    against **147.01** (34x). Both are far above `A_DECISIVE_MARGIN`, which is
    the point of quoting them — the constant separates *decided* from *tied*,
    and a future fixture trimmed for speed would show up here as the margin
    collapsing rather than as a plan that silently flipped.

    `gen_random_uuid()` rather than `new_id()`: these are filler rows for the
    planner and never leave the transaction, and one `INSERT ... SELECT` beats
    5,000 round trips. Identity is still UUIDv7 everywhere it is a domain fact.
    """
    statements = compliance_panel_sql()
    await session.execute(
        text(
            "INSERT INTO raw_payloads (id, provider, kind, reference, payload, fetched_at) "
            "SELECT gen_random_uuid(), 'tmdb', 'movie', i::text, '{}'::jsonb, "
            "now() - CAST(i || ' minutes' AS interval) "
            "FROM generate_series(1, :rows) AS i"
        ),
        {"rows": _PLAN_ROWS},
    )
    seeded = (
        await session.execute(text("SELECT count(*) FROM raw_payloads WHERE provider = 'tmdb'"))
    ).scalar_one()
    assert seeded == _PLAN_ROWS, f"the plan fixture is {seeded} rows, not {_PLAN_ROWS}"
    await analyze("raw_payloads")

    for target, statement in (("the oldest entry", statements[0]), ("the count", statements[1])):
        plan = await _explain(session, statement)
        assert "ix_raw_payloads_fetched_at" in plan, f"{target} does not use the index:\n{plan}"
        async with index_suspended(session, "ix_raw_payloads_fetched_at"):
            runner_up = await _explain(session, statement)
        assert "ix_raw_payloads_fetched_at" not in runner_up, (
            f"suspending the index left it in {target}'s plan, so the margin below compares "
            f"the plan to itself:\n{runner_up}"
        )
        assert total_cost(runner_up) > total_cost(plan) * A_DECISIVE_MARGIN, (
            f"{target} is a tie-break rather than a property of the schema -- chosen "
            f"{total_cost(plan)}:\n{plan}\nnext best {total_cost(runner_up)}:\n{runner_up}"
        )

    denominator = await _explain(session, statements[2])
    assert "ix_raw_payloads_fetched_at" not in denominator, (
        "the denominator now uses the fetched_at index, which no index on fetched_at alone "
        f"can serve -- count(*) reads every row. Has the panel changed shape?\n{denominator}"
    )
