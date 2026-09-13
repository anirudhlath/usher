"""PRD 10's *Cost anomaly*, executed against a real PostgreSQL."""

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from sqlalchemy import Integer, Numeric, RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import A_DECISIVE_MARGIN, Analyze, index_suspended, total_cost
from tests.unit.test_alerts import cost_anomaly_sql
from usher.db.models.curation import COST_PRECISION, COST_SCALE

# : What one ordinary night costs this deployment, and **the fixture's unit is : a
# measurement rather than a round number**.
_A_NIGHT = Decimal("0.01658700")

#: The floor, **read out of the committed statement** rather than retyped, so a
#: task that raises it cannot leave this file asserting the old number. The
#: literal multiples below are deliberately *not* derived this way -- see
#: `test_the_anomaly_query_fires_on_a_tripled_day_and_not_on_a_doubled_one`.
_THE_FLOOR = Decimal(re.findall(r">= (\d+\.\d+)\b", cost_anomaly_sql())[0])

# : Rows land at this hour UTC on every day but today, which is far enough from : both
# midnights that no arm is reading a boundary it did not mean to.
_A_MIDDAY = 12

# : How many ledger rows the plan assertion seeds, and the number is the : assertion's
# premise.
_SEEDED_LEDGER_ROWS = 4000

_SEED_AT_HOUR = text(
    "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out, cost_usd,"
    " latency_ms, ok, error, generation_id) "
    "VALUES (gen_random_uuid(),"
    "        (date_trunc('day', now() AT TIME ZONE 'UTC')"
    "         - make_interval(days => :days_back)"
    "         + make_interval(hours => :hour)) AT TIME ZONE 'UTC',"
    "        'fake:test-model', 'curation', 1200, 340, :cost, 4310, true, NULL,"
    "        gen_random_uuid())"
).bindparams(
    bindparam("days_back", type_=Integer()),
    bindparam("hour", type_=Integer()),
    # The column's own declaration, from the same two constants the model and
    # the migration read. A fixture that let a `float` through here would be
    # seeding a number the schema refuses to store and asserting on the
    # rounding.
    bindparam("cost", type_=Numeric(COST_PRECISION, COST_SCALE, asdecimal=True)),
)

_SEED_NOW = text(
    "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out, cost_usd,"
    " latency_ms, ok, error, generation_id) "
    "VALUES (gen_random_uuid(), now(), 'fake:test-model', 'curation', 1200, 340, :cost,"
    "        4310, true, NULL, gen_random_uuid())"
).bindparams(bindparam("cost", type_=Numeric(COST_PRECISION, COST_SCALE, asdecimal=True)))

# Named columns and never `INSERT INTO llm_calls SELECT ...`, for the reason
# `db/repositories/llm_call.py` gives for its own statement: a positional insert
# is correct today and silently shifts the moment a column is added.
_SEED_LADDER = text(
    "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out, cost_usd,"
    " latency_ms, ok, error, generation_id) "
    "SELECT gen_random_uuid(), now() - make_interval(hours => 6 * step),"
    "       'fake:test-model', 'curation', 1200, 340, 0.00500000, 4310, true, NULL,"
    "       gen_random_uuid() "
    "FROM generate_series(0, :rows - 1) AS step"
).bindparams(bindparam("rows", type_=Integer()))

#: A day the statement's window must not reach. Used by the window arm and by
#: nothing else, so the number is spelled where it is argued about.
_OUTSIDE_THE_WINDOW = 8


async def _seed(session: AsyncSession, rows: Sequence[tuple[int | None, Decimal]]) -> None:
    """One ledger row per `(days_back, cost)`; `days_back=None` means today.

    Today's row is written at `now()` rather than at an hour literal, which is
    the one asymmetry in this helper and is deliberate -- see `_A_MIDDAY`.
    """
    for days_back, cost in rows:
        if days_back is None:
            await session.execute(_SEED_NOW, {"cost": cost})
        else:
            await session.execute(
                _SEED_AT_HOUR, {"days_back": days_back, "hour": _A_MIDDAY, "cost": cost}
            )


async def _evaluate(session: AsyncSession, sql: str | None = None) -> RowMapping:
    """Run the committed statement (or a planted variant) and return its row.

    **One row is asserted here rather than in every case**, because it is the
    property Grafana's whole conversion rests on: a statement returning two
    rows is two alert instances, and one returning none is `NoData` -- which
    this rule answers `OK`, so a query that quietly stopped returning anything
    would read as *healthy* rather than as broken. That is the Postgres-side
    spelling of the empty-vector failure the rule file's header opens with.
    """
    result = await session.execute(text(sql if sql is not None else cost_anomaly_sql()))
    rows = result.mappings().all()
    assert len(rows) == 1, (
        f"the cost-anomaly statement returned {len(rows)} rows; Grafana reads one row as "
        "one alert instance and none as NoData, which this rule answers OK"
    )
    return rows[0]


def _planted(replacements: Mapping[str, str]) -> str:
    """The committed statement with one decision mutated.

    having checked that the mutation landed.

    `testing-discipline.md`: *"a plant that did not land looks exactly like a
    check that passed"*. Here it would look like something else and worse -- a
    `str.replace` matching nothing hands back the committed statement, the two
    answers agree, and the case fails complaining that the mutant survived,
    which sends the next reader to the statement rather than to this helper.
    """
    sql = cost_anomaly_sql()
    for old, new in replacements.items():
        assert old in sql, f"the plant did not land: {old!r} is not in the committed statement"
        sql = sql.replace(old, new)
    assert sql != cost_anomaly_sql(), "the plant left the statement unchanged"
    return sql


@pytest.mark.parametrize(
    ("multiple", "fires"),
    [(Decimal("2.9"), False), (Decimal("3.1"), True)],
    ids=["2.9x-does-not-fire", "3.1x-fires"],
)
async def test_the_anomaly_query_fires_on_a_tripled_day_and_not_on_a_doubled_one(
    session: AsyncSession, multiple: Decimal, fires: bool
) -> None:
    """🔴 The headline, and **the multiples are literal on purpose**."""
    await _seed(
        session,
        [(day, _A_NIGHT) for day in range(1, 8)] + [(None, _A_NIGHT * multiple)],
    )

    answer = await _evaluate(session)

    assert answer["days_in_window"] == "8", (
        "the premise: seven complete trailing days plus the partial one being judged. "
        f"This statement's calendar holds {answer['days_in_window']}"
    )
    assert answer["days_with_spend"] == "8", (
        "the premise: every seeded day landed *inside* the window. "
        f"{answer['days_with_spend']} of 8 carry spend"
    )
    assert answer["trailing_median_usd"] == f"{_A_NIGHT:.8f}", (
        "the trailing median the database computed is not the one the fixture intended, "
        "so the multiple below is being measured against the wrong bar"
    )
    assert Decimal(answer["today_spend_usd"]) > _THE_FLOOR * 2, (
        f"the premise: this arm measures the multiplier, not the floor. Today's "
        f"{answer['today_spend_usd']} is not clear of the floor {_THE_FLOOR}"
    )
    assert answer["spend_ratio"] == f"{multiple:.4f}", (
        f"the ratio the statement reports is not the one this arm seeded: {answer}"
    )

    assert answer["fired"] == int(fires), (
        f"today's {answer['today_spend_usd']} against a trailing median of "
        f"{answer['trailing_median_usd']} is {answer['spend_ratio']}x, and PRD 10's "
        f"condition is > 3x: expected fired={int(fires)}, got {answer['fired']}"
    )


@pytest.mark.parametrize(
    ("nights", "fires"),
    [(1, False), (4, True)],
    ids=["one-ordinary-night-under-the-floor", "four-nights-over-it"],
)
async def test_a_zero_trailing_median_is_held_by_the_floor_and_not_by_the_comparison(
    session: AsyncSession, nights: int, fires: bool
) -> None:
    """🔴 The state every unpriced deployment is in.

    and the one the comparison alone gets catastrophically wrong.
    """
    await _seed(session, [(None, _A_NIGHT * nights)])

    answer = await _evaluate(session)

    assert answer["days_in_window"] == "8", answer
    assert answer["days_with_spend"] == "1", (
        "the premise: this arm seeds today and nothing else, so exactly one day in the "
        f"window carries spend. The statement counts {answer['days_with_spend']}"
    )
    assert answer["trailing_median_usd"] == "0.00000000", (
        "the premise: seven trailing days with no calls have to read as seven *zeros*, "
        "not as an empty set -- a median over no rows is NULL and every comparison "
        f"against it is NULL, which is a third answer this alert has no state for: {answer}"
    )
    assert answer["spend_ratio"] == "undefined (zero trailing median)", answer
    assert answer["floor_usd"] == f"{_THE_FLOOR:.8f}", answer

    assert answer["fired"] == int(fires), (
        f"{nights} night(s) at {answer['today_spend_usd']} against a zero trailing median "
        f"and a floor of {answer['floor_usd']}: expected fired={int(fires)}, got "
        f"{answer['fired']}"
    )


async def test_deleting_the_floor_pages_every_unpriced_deployment_on_its_first_priced_night(
    session: AsyncSession,
) -> None:
    """The floor's teeth, shown by executing the statement without it.

    The arm above asserts that one ordinary night does not fire. On its own
    that assertion is satisfied by a great many statements -- including one
    whose floor is `0`, if the comparison happened to be false for another
    reason. Here the *same* fixture is run through a statement with the floor
    deleted and the answer flips, which is the only evidence that the floor is
    what produced the first answer.

    Without it the alert pages on the first cent ever spent, forever, on the
    majority of deployments -- and it is the deployment that has just been
    configured correctly that gets paged, on the night it started working.
    """
    await _seed(session, [(None, _A_NIGHT)])
    floorless = _planted({f">= {_THE_FLOOR}": ">= 0"})

    committed = await _evaluate(session)
    without = await _evaluate(session, floorless)

    assert committed["today_spend_usd"] == without["today_spend_usd"] == f"{_A_NIGHT:.8f}", (
        "the two statements are being scored against different spends, so the difference "
        f"below is not the floor: {committed} vs {without}"
    )
    assert committed["fired"] == 0, committed
    assert without["fired"] == 1, (
        "deleting the floor did not change the answer on the one fixture it exists for, "
        f"so this arm is not measuring the floor at all: {without}"
    )


async def test_the_trailing_statistic_is_a_median_and_a_mean_would_miss_this_night(
    session: AsyncSession,
) -> None:
    """🔴 PRD 10 says median, and **a flat trailing week ratifies the mean**."""
    week = [
        (7, Decimal(0)),
        (6, Decimal(0)),
        (5, _A_NIGHT),
        (4, _A_NIGHT),
        (3, _A_NIGHT),
        (2, _A_NIGHT),
        (1, _A_NIGHT * 6),
    ]
    await _seed(
        session, [(day, cost) for day, cost in week if cost] + [(None, _A_NIGHT * Decimal("3.5"))]
    )
    averaged = _planted(
        {"percentile_disc(0.5) WITHIN GROUP (ORDER BY daily.spend)": "avg(daily.spend)"}
    )

    committed = await _evaluate(session)
    mean = await _evaluate(session, averaged)

    assert committed["days_in_window"] == mean["days_in_window"] == "8", committed
    assert committed["days_with_spend"] == "6", (
        "the premise: two of the seven trailing nights are silent, so six of the eight "
        f"days in the window carry spend. The statement counts {committed['days_with_spend']}"
    )
    assert committed["trailing_median_usd"] == f"{_A_NIGHT:.8f}", (
        f"the median of this week is one ordinary night: {committed}"
    )
    assert mean["trailing_median_usd"] != committed["trailing_median_usd"], (
        "the mean and the median agree on this fixture, so it cannot tell them apart and "
        f"the assertion below would pass for either statistic: {mean}"
    )

    assert committed["fired"] == 1, committed
    assert mean["fired"] == 0, (
        "a mean over this week does not page on a night that cost three and a half times "
        f"the median, and the committed statement does: {mean}"
    )


async def test_the_window_is_seven_complete_trailing_days_and_reaches_no_further(
    session: AsyncSession,
) -> None:
    """🔴 Eight calendar days: seven complete ones judged, plus the partial one being judged.

    Both ends are asserted, and by the same fixture.
    """
    # `(days_back, multiple of one night)`, so the *first* entry is the oldest
    # day in the window and is deliberately the largest -- an ascending week
    # would leave the median where it was under both plants.
    week = [(7, 7), (6, 1), (5, 2), (4, 3), (3, 4), (2, 5), (1, 6)]
    await _seed(
        session,
        [(_OUTSIDE_THE_WINDOW, _A_NIGHT / 2)]
        + [(day, _A_NIGHT * multiple) for day, multiple in week]
        + [(None, _A_NIGHT * 11)],
    )
    narrowed = _planted({"interval '7 days'": "interval '6 days'"})
    widened = _planted({"interval '7 days'": "interval '8 days'"})

    committed = await _evaluate(session)
    six = await _evaluate(session, narrowed)
    eight = await _evaluate(session, widened)

    assert committed["days_in_window"] == "8", committed
    assert committed["days_with_spend"] == "8", (
        "the premise: the day seeded outside the window must not be in it, and all eight "
        f"inside it must be. The statement counts {committed['days_with_spend']}"
    )
    assert (six["days_in_window"], eight["days_in_window"]) == ("7", "9"), (
        f"the two plants did not move the window: {six}, {eight}"
    )
    assert committed["trailing_median_usd"] == f"{_A_NIGHT * 4:.8f}", (
        f"the median of the seven in-window days is not four nights: {committed}"
    )
    assert Decimal(committed["today_spend_usd"]) > _THE_FLOOR, (
        "the premise: this arm measures the window, and the floor must not be what "
        f"decides it: {committed}"
    )

    assert committed["fired"] == 0, (
        f"today's {committed['today_spend_usd']} is {committed['spend_ratio']}x the "
        "trailing median, which is under 3x"
    )
    assert six["fired"] == 1, (
        "a window one day short leaves the median where it was on this fixture, so the "
        f"narrowing is invisible and this arm has no teeth: {six}"
    )
    assert eight["fired"] == 1, (
        "a window one day long reaches a ledger row that is not part of this week and "
        f"the median does not move, so nothing here would notice: {eight}"
    )


async def test_the_comparison_stays_in_numeric_and_float8_pages_on_an_exact_tie(
    session: AsyncSession,
) -> None:
    """🔴 `cost_usd` is `NUMERIC(12, 8)` and the ratio has to stay there."""
    exactly_three = Decimal("0.14500000")
    await _seed(
        session,
        [(day, exactly_three) for day in range(1, 8)] + [(None, exactly_three * 3)],
    )
    floated = _planted(
        {
            "judged.today_spend > 3 * judged.trailing_median": (
                "judged.today_spend::float8 > 3 * judged.trailing_median::float8"
            )
        }
    )

    committed = await _evaluate(session)
    binary = await _evaluate(session, floated)

    assert committed["trailing_median_usd"] == "0.14500000", committed
    assert committed["today_spend_usd"] == "0.43500000", committed
    assert committed["spend_ratio"] == binary["spend_ratio"] == "3.0000", (
        "the premise: this is an *exact* tie at three times, so the two statements agree "
        f"on every number they report and differ only in the verdict: {committed}, {binary}"
    )

    assert committed["fired"] == 0, (
        f"PRD 10's condition is *greater than* 3x and this day is exactly 3x: {committed}"
    )
    assert binary["fired"] == 1, (
        "casting the comparison to `double precision` did not change the answer on an "
        "exact tie, so this arm is not measuring the type -- respell the plant"
    )

    # The type claim itself, read out of the database rather than asserted from
    # the documentation, because it is the whole reason `percentile_disc` is
    # spelled here at all.
    types = (
        await session.execute(
            text(
                "SELECT pg_typeof(percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_usd))::text,"
                "       pg_typeof(percentile_disc(0.5) WITHIN GROUP (ORDER BY cost_usd))::text "
                "FROM llm_calls"
            )
        )
    ).one()
    assert tuple(types) == ("double precision", "numeric"), (
        "PostgreSQL's percentile overloads are not what this statement was written "
        f"against: {types}"
    )


@pytest.mark.parametrize(
    "elsewhere", ["Pacific/Kiritimati", "Pacific/Midway"], ids=["UTC+14", "UTC-11"]
)
async def test_the_day_boundary_does_not_move_with_the_sessions_time_zone(
    session: AsyncSession, elsewhere: str
) -> None:
    """`date_trunc('day'.

    <timestamptz>)` truncates in the **session's** time zone, so the unqualified
    spelling makes "today" a property of who is asking.
    """
    await _seed(session, [(day, _A_NIGHT) for day in range(1, 8)])
    for hour, cost in ((5, Decimal("0.02000000")), (20, Decimal("0.03000000"))):
        await session.execute(_SEED_AT_HOUR, {"days_back": 0, "hour": hour, "cost": cost})
    unqualified = _planted({" AT TIME ZONE 'UTC'": ""})

    here = dict(await _evaluate(session))
    here_unqualified = dict(await _evaluate(session, unqualified))
    # `SET LOCAL` is scoped to this transaction, which the `session` fixture
    # rolls back -- so nothing here reaches the next test or the container's
    # other connections.
    await session.execute(text(f"SET LOCAL TIME ZONE '{elsewhere}'"))
    there = dict(await _evaluate(session))
    there_unqualified = dict(await _evaluate(session, unqualified))

    assert here["days_with_spend"] == "8", (
        f"the premise: seven trailing nights plus today, all inside the window: {here}"
    )
    assert here_unqualified != there_unqualified, (
        f"the premise: the unqualified spelling has to be *able* to disagree, or the "
        f"agreement asserted below is a property of the container's time zone rather "
        f"than of the statement. In {elsewhere} it answered {there_unqualified}"
    )

    assert here == there, (
        f"the committed statement answers differently in {elsewhere} than in UTC, so "
        f"'today' depends on how the Grafana server was started: {here} vs {there}"
    )


async def test_the_windows_lower_bound_is_served_by_the_time_index(
    session: AsyncSession, analyze: Analyze
) -> None:
    """**`ix_llm_calls_at` earns its keep on this statement too**.

    which is the other half of the sentence `m08a` deferred it with.
    """
    await session.execute(_SEED_LADDER, {"rows": _SEEDED_LEDGER_ROWS})
    # Without statistics the planner sizes `llm_calls` off an empty `pg_class`,
    # every candidate costs the same to four significant figures, and which one
    # it names is decided by nothing this test controls.
    await analyze("llm_calls")
    seeded = await session.execute(text("SELECT count(*) FROM llm_calls"))
    assert seeded.scalar_one() == _SEEDED_LEDGER_ROWS, (
        "the premise: the plan below is asserted at a size where the index can win"
    )

    plan = await _explain(session)
    assert "Index Scan using ix_llm_calls_at on llm_calls" in plan, plan
    assert "Index Cond: (at >= " in plan, (
        f"the window's lower bound did not become this index's condition, so the alert "
        f"reads rows it is going to throw away:\n{plan}"
    )

    async with index_suspended(session, "ix_llm_calls_at"):
        runner_up = await _explain(session)
    assert "ix_llm_calls_at" not in runner_up, (
        f"the index was not actually suspended, so the comparison below measures "
        f"nothing:\n{runner_up}"
    )
    assert total_cost(runner_up) > total_cost(plan) * A_DECISIVE_MARGIN, (
        f"the time index wins by too little for this to be a property of the schema "
        f"rather than of tie-breaking order -- the fixture is back below the scale at "
        f"which the planner can tell the candidates apart, at {_SEEDED_LEDGER_ROWS} "
        f"rows.\nchosen {total_cost(plan)}:\n{plan}\n"
        f"next best {total_cost(runner_up)}:\n{runner_up}"
    )

    # **And the statement is executed afterwards.** A plan measured against a
    # query nobody runs is a plan for a query that may not answer correctly --
    # `test_the_windowed_read_is_served_by_the_time_index` takes the same care
    # one module over, for the same reason.
    answer = await _evaluate(session)
    assert answer["days_in_window"] == "8", (
        f"the statement that planned well answers about the wrong window: {answer}"
    )


async def test_the_statement_returns_one_row_and_one_numeric_column(
    session: AsyncSession,
) -> None:
    """🔴 Grafana's SQL-to-alerting conversion reads a table frame as *one series per numeric.

    column, labelled by every string column*, and the condition on this rule is a `> 0`
    threshold.
    """
    await session.execute(text(f"CREATE TEMP VIEW cost_anomaly AS {cost_anomaly_sql()}"))
    columns = (
        (
            await session.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'cost_anomaly' ORDER BY ordinal_position"
                )
            )
        )
        .mappings()
        .all()
    )

    assert [column["column_name"] for column in columns] == [
        "fired",
        "today_spend_usd",
        "trailing_median_usd",
        "spend_ratio",
        "floor_usd",
        "days_in_window",
        "days_with_spend",
    ], columns
    numeric = [
        column["column_name"]
        for column in columns
        if column["data_type"] not in {"text", "character varying"}
    ]
    assert numeric == ["fired"], (
        "every column but `fired` has to reach Grafana as a label, and a numeric one "
        f"becomes a second series the `> 0` threshold judges: {columns}"
    )

    # And the value is 0 or 1 rather than anything else a `> 0` would also
    # accept -- an empty ledger being the state this deployment is actually in.
    answer = await _evaluate(session)
    assert answer["fired"] == 0 and answer["today_spend_usd"] == "0.00000000", (
        f"an empty ledger is not an anomaly, and this is what the alert says about one: {answer}"
    )


async def _explain(session: AsyncSession) -> str:
    """`EXPLAIN` in text format.

    because `total_cost` reads the root node's `(cost=start..total ` off the first line.

    `EXPLAIN` without `ANALYZE`: what is asserted is the plan the planner
    *chose*, and executing it would add runtime to a comparison whose whole
    point is the estimate the two candidates were ranked on.
    """
    result = await session.execute(text("EXPLAIN " + cost_anomaly_sql()))
    return "\n".join(str(row[0]) for row in result)
