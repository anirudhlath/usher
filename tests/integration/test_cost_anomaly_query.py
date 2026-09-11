"""PRD 10's *Cost anomaly*, executed against a real PostgreSQL.

**The alert's subject is a table, not a series, and that is the whole reason
this file exists.** Six of PRD 10's seven alerts are PromQL, and
`tests/unit/test_alerts.py` grades them the only way a unit test can: by
reading the expression and asking whether it could ever select anything.
Nothing in this repository evaluates PromQL. The seventh is a `SELECT`, and a
`SELECT` this repository *can* run -- so the grading moves from "is this
spelled like a query that works" to "does this query answer the question PRD
10 asked", which is a strictly stronger claim and the one an operator is
relying on at 2 a.m.

**Every case here executes the committed statement**, read out of
`dashboards/alerts/grafana/usher.yml` by `cost_anomaly_sql()` and never
retyped -- the shape `db/repositories/llm_call.py` uses for `_LIST_SINCE_SQL`,
and for the same reason: a statement measured in one file and shipped from
another is a statement whose copy is what stops tracking the original.

🔴 **And most of them execute a *planted* variant beside it.** The four
decisions in this query -- eight calendar days, a median rather than a mean,
an absolute floor, and arithmetic that stays in `numeric` -- are each one
token wide, and each reads perfectly correct alone. A case that only asserted
the right answer would pass with any of them deleted on some fixture, which is
the failure `.claude/rules/mutation-sweeps.md` records at `TICKET_TTL_SECONDS`,
`CAST_LIMIT` and `SimilarityService._WEIGHTS` -- three constants in one
milestone, each pinned by a case that moved both sides together. So the arms
here fix the fixture and move the *statement*, and assert the two disagree.
Every plant is checked to have landed before it is scored, because a
`str.replace` that matched nothing produces the committed statement back and
"they disagree" then fails for a reason that has nothing to do with the claim.

⚠️ **What this file cannot check.** It does not run Grafana, so the
table-frame-to-alert-instance conversion -- one numeric column becomes the
series, every string column becomes a label -- is asserted structurally in
`tests/unit/test_alerts.py` and measured for real against a throwaway Grafana,
recorded in `dashboards/README.md`. What *is* here is that the statement
returns exactly one row with exactly one numeric column, which is the property
that conversion depends on.
"""

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from sqlalchemy import Integer, Numeric, RowMapping, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import A_DECISIVE_MARGIN, Analyze, index_suspended, total_cost
from tests.unit.test_alerts import cost_anomaly_sql
from usher.db.models.curation import COST_PRECISION, COST_SCALE

#: What one ordinary night costs this deployment, and **the fixture's unit is
#: a measurement rather than a round number**.
#:
#: `02-data-model.md`'s `llm_calls` row: *"`cost_usd` verified exact end to end
#: on 2026-08-07: `0.00000000` against a local model, `0.01658700` with prices
#: configured -- exactly `Decimal((4359x3 + 234x15) / 1e6)` -- and `SUM()`
#: agrees to 8 decimal places."* That is one generation, at the `3`/`15` USD
#: per Mtok pair the live verification used, and PRD 06's shape is one
#: generation per household per night.
#:
#: Using it here is what makes the floor arm mean anything: the floor exists to
#: separate "an operator priced their model today" from "tonight cost three
#: times what a night costs", and those two are only distinguishable against a
#: real night's price. A fixture of `1.00` would clear the floor by fifty times
#: and prove only that a large number is larger than a small one.
_A_NIGHT = Decimal("0.01658700")

#: The floor, **read out of the committed statement** rather than retyped, so a
#: task that raises it cannot leave this file asserting the old number. The
#: literal multiples below are deliberately *not* derived this way -- see
#: `test_the_anomaly_query_fires_on_a_tripled_day_and_not_on_a_doubled_one`.
_THE_FLOOR = Decimal(re.findall(r">= (\d+\.\d+)\b", cost_anomaly_sql())[0])

#: Rows land at this hour UTC on every day but today, which is far enough from
#: both midnights that no arm is reading a boundary it did not mean to. Today's
#: row is written at `now()` instead: an hour literal would be in the *future*
#: for part of every day, which is a legal ledger row (`llm_calls.at` is
#: written by the caller and carries no `server_default`) but would make the
#: time-zone arm below argue with the clock rather than with the statement.
_A_MIDDAY = 12

#: How many ledger rows the plan assertion seeds, and the number is the
#: assertion's premise. **Measured 2026-09-11 on PostgreSQL 17.10**
#: (`pgvector/pgvector:pg17`), seeding one row every six hours back from `now()`
#: so the statement's own eight-day window selects **32 rows whatever the table
#: holds** and only the relation's size varies. Each row is `EXPLAIN` of the
#: committed statement with `ix_llm_calls_at` available against `EXPLAIN` with
#: it suspended, so the ratio compares this index against the *best
#: alternative* rather than against an assumption:
#:
#: | seeded rows | plan chosen | chosen | next best | ratio |
#: |---|---|---|---|---|
#: | 100   | `Seq Scan`   | 46.42 | 46.42  | **1.00** |
#: | 300   | `Index Scan` | 50.64 | 54.42  | **1.07** |
#: | 1,000 | `Index Scan` | 50.77 | 83.92  | **1.65** |
#: | 2,000 | `Index Scan` | 50.77 | 124.92 | 2.46 |
#: | 4,000 | `Index Scan` | 50.77 | 208.80 | 4.11 |
#:
#: 🔴 **Three bold rows, not one, and the middle two are the danger.** At 100
#: rows the planner picks `Seq Scan` and is right to -- the relation is a
#: handful of pages. At 300 it already picks the index, so a case asserting
#: only the plan's *name* would be **green there**, on a margin of **1.07** --
#: a tie-break wearing a measurement's clothes, and an even flatter one than
#: the 1.17 `_SEEDED_LEDGER_ROWS` records for `list_since`. At 1,000 the margin
#: is still **1.65**, below the 2.0 `A_DECISIVE_MARGIN` demands. 4,000 reads
#: 4.11, which clears it twice over.
#:
#: ⚠️ **The whole-plan ratio understates the index by design.** Both plans
#: carry the same ~42 of CTE-scan cost for the calendar series and the
#: aggregate, which is identical work either way; the scan node alone reads
#: `9.01` against `166.16` at 4,000, **18.4x**. The assertion is on the root
#: cost because that is what `total_cost` reads and what the sibling plan cases
#: compare, and because the diluted number is the conservative one.
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
    """The committed statement with one decision mutated, having checked that
    the mutation landed.

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
    """🔴 The headline, and **the multiples are literal on purpose**.

    PRD 10's condition is *"Daily LLM spend > 3x trailing 7-day median"*, so
    the number under test is `3`. A case spelling its fixture as
    `3 * median +/- epsilon` moves both sides of the comparison together: it
    pins that *a* multiplier is in force and pins nothing at all about its
    value, so `2` and `10` both pass it. `.claude/rules/mutation-sweeps.md`
    records that shape costing this project three constants in one milestone.
    `2.9` and `3.1` are written out here, and against a flat trailing week at
    one measured night's price they land either side of `3` by about a third of
    a cent -- 0.04810230 and 0.05141970 against a bar of 0.04976100.

    **The premises are read back out of the database, not computed in Python.**
    The trailing median is asserted as the string Postgres renders, because a
    median computed in Python from the values the fixture *passed* cannot
    notice a day boundary that put one of them in the wrong bucket -- and the
    two disagree the moment a time zone does. `days_in_window` and
    `days_with_spend` are the statement's own answer about its window, so a
    seed that landed outside it fails here rather than passing with a median
    over fewer days than it thinks.

    **And the floor is asserted not to be what decided**, which is the arm's
    other premise: both today values clear it by more than double, so this case
    is measuring the multiplier alone. Without that assertion a later edit
    raising the floor above 0.0514 would turn both arms green for the wrong
    reason -- the 3.1 arm would stop firing and the parametrisation would
    report a failure whose message named the multiple.
    """
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
    """🔴 The state every unpriced deployment is in, and the one the comparison
    alone gets catastrophically wrong.

    A trailing median of `0` makes `3 x median` zero, and **any** spend is then
    greater than it. That is not an edge case: both per-million-token prices
    default to `Decimal(0)` (`src/usher/config.py`'s `llm_price_in_per_mtok`
    and `llm_price_out_per_mtok`), which that file calls the honest value for a
    local model and the wrong one for a hosted model an operator forgot to
    price -- and this host's LLM is a local vLLM. So the majority state is a
    week of `0.00000000` nights, and the day an operator finally fills those
    two settings in, the first priced generation is infinitely more than the
    trailing median.

    **The floor is what that costs, and the two arms are what make it a floor
    rather than a mute button.** One ordinary night (0.01658700, the 2026-08-07
    measurement) is below 0.02 and does not page. Four nights in one evening
    (0.06634800) is above it and does, against the same zero median -- so a
    deployment that really did spend four times its usual on the first day it
    had prices still gets told. `testing-discipline.md`: *"has any fixture, in
    either arm, ever written the other value?"*

    **`spend_ratio` is a sentence, not a number, and that is the zero-versus-
    absence rule applied to the page itself.** A ratio against a zero median is
    undefined, and rendering it as `0.0000` or `Infinity` would put a number in
    front of an operator that means neither "no anomaly" nor "an enormous one".
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
    """🔴 PRD 10 says median, and **a flat trailing week ratifies the mean**.

    That is the trap in this arm and it is worth stating before the fixture:
    over seven equal days `avg` and `percentile_disc(0.5)` return the same
    number, so the parametrised case above -- which needs a flat week to make
    its 2.9/3.1 literals mean anything -- cannot tell the two apart. Neither
    can the fixture the task text prescribes for this arm, *"one zero day and
    one double day"*: over `[0, N, N, N, N, N, 2N]` the mean is `7N/7 = N` and
    the median is `N` **exactly**, so that week ratifies the mean too.

    So this week is `[0, 0, N, N, N, N, 6N]` -- two silent nights and one
    re-run that cost six -- where the median is `N` and the mean is `10N/7`,
    about 1.43 times it. Today spent `3.5N`, which is over the median's bar of
    `3N` and under the mean's of `30N/7`. The committed statement pages; the
    planted one is silent about a night that cost three and a half times a
    normal one.

    **And that is the failure mode PRD 10's choice is about**, rather than a
    statistical preference: this deployment makes one generation per household
    per night, so a single failed night at $0 and a single re-run at 2x are
    both *ordinary*, and a mean carries both of them into the bar. A median
    does not move until half the week does.
    """
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
    """🔴 Eight calendar days: seven complete ones judged, plus the partial one
    being judged. Both ends are asserted, and by the same fixture.

    **Why eight and not seven.** The trailing median excludes today, so a
    seven-day window would hold six complete days and one partial. A window
    that instead included today in the median compares today against a bar it
    is a member of, which moves toward whatever fired it.

    **Why a median makes this hard to pin, and how this fixture does it.**
    Dropping the *lowest* trailing day from seven values leaves the median
    where it was -- that is what a median is for -- so the obvious fixture
    (an ascending week) cannot see the window narrow. Here the oldest
    in-window day is the **largest**: `[7N, 1N, 2N, 3N, 4N, 5N, 6N]` from
    oldest to newest, whose median is `4N`. Drop the oldest and it falls to
    `3N`. Add the day before it -- `0.5N`, seeded outside the window on purpose
    -- and it also falls to `3N`. Today spent `11N`, which is under the correct
    bar of `12N` and over the mutants' `9N`, so **both** the narrowed and the
    widened window page on a night the committed statement correctly ignores.

    A spurious page is the right direction to test in: an alert that fires on
    an ordinary Tuesday is one an operator turns off, after which it is not an
    alert at all.
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
    """🔴 `cost_usd` is `NUMERIC(12, 8)` and the ratio has to stay there.

    `02-data-model.md`'s `llm_calls` row: *"never a float: `$3/Mtok x 1,200
    tokens` is exactly `0.0036` and at scale 4 a `$0.02/Mtok` call stores as
    `0.0000` -- measured."* The usual objection is that at these magnitudes a
    `double precision` carries fifteen significant digits and loses nothing
    visible, which is true -- and is not the failure. **The failure is at the
    boundary**, which is the only place a threshold alert ever is.

    A trailing median of exactly `0.14500000` and a today of exactly
    `0.43500000` is exactly three times, and PRD 10's condition is *greater
    than* three times, so the honest answer is silence. In binary floating
    point `3 * 0.145` is `0.43499999999999994`, which `0.435` is greater than.
    So the float spelling **pages on a day that is exactly, and not more than,
    three times the median** -- and it does it on the day an operator would
    least believe it, when the ratio the page carries reads `3.0000` either
    way. Both statements report the identical `spend_ratio` here; only `fired`
    differs, which is what makes this indistinguishable from a real firing
    unless somebody knows to look.

    ⚠️ **`percentile_cont` is this cast, arriving without anyone writing
    `::float8`.** Postgres has no `numeric` overload of it: measured with
    `pg_typeof` on PostgreSQL 17.10, 2026-09-11, `percentile_cont(0.5) WITHIN
    GROUP (ORDER BY <numeric>)` is `double precision` and `percentile_disc` of
    the same is `numeric`. Over the seven trailing days -- a set whose size is
    fixed at seven by the generated calendar, so always odd -- both return the
    same element. That is why the statement that shipped is not the one the
    task text supplies.
    """
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
    """`date_trunc('day', <timestamptz>)` truncates in the **session's** time
    zone, so the unqualified spelling makes "today" a property of who is asking.

    Nothing in this repository sets that session. Grafana's PostgreSQL
    datasource inherits whatever the server was started with, and a Grafana
    restarted with a different `TZ`, or a server whose `timezone` is changed,
    silently re-buckets every night in this alert's window.

    **Two of today's rows are seeded at 05:00 and 20:00 UTC, and the pair is
    what makes the disagreement independent of the clock.** A shift of `o`
    hours re-buckets a row *relative to `now()`* exactly when one of them
    crosses a local midnight and the other does not -- for `+14` that is
    `h >= 10` and for `-11` it is `h < 11`, and 05:00 and 20:00 sit either side
    of both. So whatever hour the suite runs at, exactly one of the two lands
    on a different local day from `now()` and the unqualified spelling reports
    a different day's spend. Without that pair this case would pass or fail by
    the time of day, which is the worst of both.

    The committed statement says `AT TIME ZONE 'UTC'` in both places and is
    asserted byte-identical between UTC and each of two zones 25 hours apart.
    The plant is the whole qualification removed, and it is asserted to
    *disagree* -- an arm that only checked the committed statement would pass
    just as happily against a database that happened to be running in UTC,
    which this one is (`SHOW timezone` is `Etc/UTC` on
    `pgvector/pgvector:pg17`).

    ⚠️ The window bound stays on the raw `timestamptz` column, so this
    qualification costs nothing at the index:
    `test_the_windows_lower_bound_is_served_by_the_time_index` is the case that
    says so.
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
    """**`ix_llm_calls_at` earns its keep on this statement too**, which is the
    other half of the sentence `m08a` deferred it with.

    That migration wrote the DDL out by name and said what it was for:
    *"dashboard 5's 'LLM spend per day and month' and the cost-anomaly alert
    ('daily spend > 3x the trailing 7-day median'), both `WHERE at >=
    :since`"*. D3 shipped the reader for the first half and
    `test_the_windowed_read_is_served_by_the_time_index` asserts its plan.
    This is the second half, and it is the one the index was actually named
    for -- `list_since` is `src/`'s consumer, but the alert is the query the
    docstring quotes.

    🔴 **The seeded size is the assertion's premise**, and
    `_SEEDED_LEDGER_ROWS` carries the measured ladder that picked it. The short
    version: at 300 rows the planner already chooses this index, on a margin of
    **1.07**, and at 1,000 it is still only **1.65** -- under the 2.0
    `A_DECISIVE_MARGIN` calls decided. A case asserting the plan's name alone
    would be green at 300 and would be reporting tie-breaking order, which is
    the shape of issue #79's two CI failures.

    **The `AT TIME ZONE 'UTC'` qualification is on the *truncation*, never on
    the column**, and this case is what holds that apart: `llm_calls.at AT TIME
    ZONE 'UTC'` in the `GROUP BY` is a computed expression no btree over `at`
    can serve, while the `WHERE` compares the raw column against a stable
    expression. If somebody "tidies" the two into one spelling, the `Index
    Cond` below becomes a `Filter` and the alert starts reading the whole
    ledger every ten minutes.
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
    """🔴 Grafana's SQL-to-alerting conversion reads a table frame as *one
    series per numeric column, labelled by every string column*, and the
    condition on this rule is a `> 0` threshold.

    So a second numeric column is not a cosmetic slip: `days_in_window` is
    `8` by construction, and returned as a number it would be a second series
    that clears `> 0` on every evaluation, pinning this alert firing forever
    with a page that names no anomaly. That is the Postgres-side twin of the
    empty-vector failure `dashboards/alerts/usher.yml`'s header opens with,
    and it fails in the louder direction rather than the silent one -- but it
    fails to an operator who then turns the alert off.

    The types are read out of `information_schema` through a temporary view
    over the committed statement, which is the database's own answer about
    what it will hand a client. Asserting the `::text` casts in the SQL text is
    the unit test's job (`test_the_cost_anomaly_rule_hands_grafana_exactly_one_
    numeric_column`); this is the arm that would notice a cast that is present
    and does not do what it looks like.

    ⚠️ **The view is created inside the test's own transaction and dies with
    the rollback.** `CREATE TEMP VIEW` is transactional DDL in PostgreSQL, and
    a temporary object is scoped to the connection besides.
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
    """`EXPLAIN` in text format, because `total_cost` reads the root node's
    `(cost=start..total ` off the first line.

    `EXPLAIN` without `ANALYZE`: what is asserted is the plan the planner
    *chose*, and executing it would add runtime to a comparison whose whole
    point is the estimate the two candidates were ranked on.
    """
    result = await session.execute(text("EXPLAIN " + cost_anomaly_sql()))
    return "\n".join(str(row[0]) for row in result)
