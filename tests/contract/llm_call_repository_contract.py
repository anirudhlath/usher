"""Behaviour every `LLMCallRepository` implementation must satisfy.

The port has **an append and one windowed read**, and `record()` is called on
both the path where a generation worked and the path where it did not -- a
ledger holding only the successes understates spend by exactly the failures,
which are the rows an operator most wants to see -- and `ok` is the
discriminator rather than "the HTTP call returned 200".

**A write is still observed through an abstract `LLMCallLedger` rather than
through `list_since`, and M10 made that a choice rather than a necessity.**
Until this milestone the port had no read at all and the ledger was the only
way to see anything; `list_since` now exists, and every case below that
predates it still reads through `LLMCallLedger` on purpose. Two reasons. A
read observed through itself cannot fail -- a `record()` that dropped
`generation_id` and a `list_since` that never selected it agree perfectly, and
the round trip would be green against both. And `list_since` is *windowed and
ordered*, so it answers a question about a slice; `get(id)` and `count()`
answer questions about the row and the table, which is what the write cases
need. The three read cases at the end of this suite are the ones whose subject
*is* `list_since`, and they are the only ones that call it.

Its `ABC` shape is ADR-0001's argument applied to a test double -- a
`Protocol` would let one arm drift out of the suite silently.

**Every case names the wrong implementation it rules out.** A test whose
docstring cannot name what it kills is a test that kills nothing.

**Almost every assertion here is structural against a dict-backed fake and
load-bearing against Postgres**, because the fake stores the very `LLMCall` it
was handed: no column mapping exists there to get wrong. That is the first
entry in `tests/fakes/llm_call_repository.py`'s divergence list, and it is why
this suite is run against both arms rather than against the fake alone --
`TitleNeighborRepository` is the one repository port that skipped that, and
the gap hid a live defect for a milestone.
"""

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from usher.domain.curation import LLMCall, LLMPurpose
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import LLMCallRepository

#: What `llm_calls.model` records. Invented, like every value in this suite --
#: see `tests/fixtures/README.md`.
MODEL = "fake:test-model"

#: When the completion happened, not when the row was inserted. `llm_calls.at`
#: carries no server default for exactly that reason, so a fixture that omitted
#: it would be testing a column this schema does not have.
AT = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)

#: **PRD 10's own worked example, and the three numbers are pairwise distinct
#: on purpose.** 1,200 tokens in at $3/Mtok plus 340 out at $15/Mtok is exactly
#: $0.0087 -- a value binary floating point cannot represent, which is why
#: `cost_usd` is a `Decimal` and the column is `NUMERIC(12, 8)`. The distinctness
#: is what makes a write that fills `tokens_out` from `tokens_in`, or
#: `latency_ms` from either, visible at all; the premise is asserted in the case
#: rather than trusted here.
TOKENS_IN = 1200
TOKENS_OUT = 340
LATENCY_MS = 4310
COST = Decimal("0.0087")

#: The measured values from `m08a`'s own table, which is where the column's
#: scale came from. `0.00000002` is `$0.02/Mtok x 1 token` and is the one that
#: a `NUMERIC(12, 6)` stores as `0.000000` -- a real call reported as free.
#: `0` is not a placeholder either: both prices default to `0`, so an operator
#: who never priced their model produces this row on every call.
MEASURED_COSTS = [
    Decimal("0.0036"),
    Decimal("0.0087"),
    Decimal("0.00000002"),
    Decimal("1.92"),
    Decimal("0"),
]

#: The window `list_since` is asked for, and the four timestamps around it.
#:
#: **Every one of these is a literal, and not one is written as `WINDOW_START ±
#: something`.** A fixture spelled as an offset from the bound it is testing
#: moves *with* the bound: narrow `>=` to `>` and a row placed at
#: `WINDOW_START + timedelta(0)` is still computed from whatever the predicate
#: became, so the case ratifies that *a* bound exists while pinning nothing
#: about which one. `.claude/rules/mutation-sweeps.md` records three constants
#: in one milestone lost that way -- `TICKET_TTL_SECONDS`, `CAST_LIMIT` and
#: `SimilarityService._WEIGHTS`.
#:
#: Two of them are deliberately *equal* to a bound rather than near it:
#: `FIRST_IN_WINDOW` is exactly `WINDOW_START`, which is the only row a `>`
#: drops, and `AFTER_THE_WINDOW` is exactly `WINDOW_END`, which is the only row
#: a `<=` keeps. A fixture that placed both an hour clear of their bounds would
#: be green under either spelling of either comparison.
WINDOW_START = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)

#: A minute before the window opens. The row every `WHERE at >= :since` must
#: leave behind, and the one an unbounded `SELECT *` returns.
BEFORE_THE_WINDOW = datetime(2026, 8, 4, 23, 59, tzinfo=UTC)

#: Exactly `WINDOW_START`. `>=` keeps it and `>` does not, and that is the
#: whole of what separates the two.
FIRST_IN_WINDOW = WINDOW_START

#: Comfortably inside, so the window has an interior and the case is not two
#: boundary rows wearing a window's clothes.
SECOND_IN_WINDOW = datetime(2026, 8, 5, 21, 0, tzinfo=UTC)

#: Exactly `WINDOW_END`. The window is half-open -- `[since, until)` -- so this
#: row belongs to the *next* window and to no other, which is what keeps a
#: spend-per-day panel from billing one call to two days.
AFTER_THE_WINDOW = WINDOW_END

#: How far past `datetime.now(UTC)` the one clock-relative fixture in this
#: module sits -- `test_a_window_with_no_end_runs_to_the_end_of_the_ledger`'s
#: row, and nothing else.
#:
#: **A day, so that no plausible skew between this process's clock and
#: Postgres's `now()` can close the gap**, since the row is written here and
#: the hypothetical `now()` it must outlive would be evaluated on the server.
#: Seconds or minutes would make the case's verdict a property of two clocks;
#: a day makes it a property of the statement.
A_CLEAR_DAY = timedelta(days=1)


def llm_call(
    *,
    generation_id: uuid.UUID | None,
    call_id: uuid.UUID | None = None,
    at: datetime = AT,
    model: str = MODEL,
    purpose: LLMPurpose = LLMPurpose.CURATION,
    tokens_in: int = TOKENS_IN,
    tokens_out: int = TOKENS_OUT,
    cost_usd: Decimal = COST,
    latency_ms: int = LATENCY_MS,
    ok: bool = True,
    error: str | None = None,
) -> LLMCall:
    """One ledger entry, with the fields a case does not care about filled in.

    **`generation_id` is required and keyword-only, with no default.** It is
    the column PRD 10's dashboard 5 joins on and the only one whose `None` is
    a distinct, meaningful state (a purpose that produces no rows at all --
    query expansion is one), so a fixture that could leave it unstated would
    make "this call belongs to no generation" reachable by accident.

    Eleven fields positionally is eleven chances to fill the wrong slot and
    still pass, which is why every parameter here is keyword-only.
    """
    return LLMCall(
        id=call_id if call_id is not None else new_id(),
        at=at,
        model=model,
        purpose=purpose,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        ok=ok,
        error=error,
        generation_id=generation_id,
    )


class LLMCallLedger(ABC):
    """The stored ledger, read without going through the port.

    Not `LLMCallRepository.list_since`, and not because the port lacks a read
    any more -- M10 gave it one. This stays an independent observer so that a
    write case cannot be satisfied by a read with the mirrored defect: a
    `record()` that dropped `generation_id` and a `list_since` that never
    selected it round-trip perfectly. See this module's docstring.

    **No `user()`, unlike `CuratedRowSeeder`.** That one exists because
    `curated_rows.user_id` is a foreign key on one arm and nothing on the
    other, so a bare UUID would exercise the conflict path against Postgres
    and the happy path against the fake. `llm_calls` has **no foreign key at
    all** -- not to `users`, and deliberately not to any generation either
    (`m08a`: the column that would be referenced is not unique, must not
    become unique, and any foreign key would let a cascade delete a cost row
    from the thing whose cost it records) -- so an invented `generation_id` is
    storable on both arms and there is nothing to seed.

    **And no `record()`-bypassing writer either**, which is the other half of
    the asymmetry with `CuratedRowSeeder`. That seeder exists because
    `replace_for_user` deletes, so no sequence of port calls can leave two
    generations stored. `record()` deletes nothing, so every state this suite
    needs is reachable through the port itself.
    """

    @abstractmethod
    async def get(self, call_id: uuid.UUID) -> LLMCall | None:
        """The stored row as stored, or `None` if there is none.

        Whole-row rather than one column, so a case can compare against the
        `LLMCall` that was handed in and catch a column dropped from the write
        or two columns filled from one another. Against Postgres this is a
        `SELECT *` into an `extra="forbid"` model built from the table's own
        column list, which is this schema's house shape and is what makes the
        comparison mechanically 1:1 with the table.
        """

    @abstractmethod
    async def count(self) -> int:
        """Every row the ledger holds.

        What makes "one row per attempt" assertable at all: `get` alone cannot
        tell a second `record()` that stored a second row from one that
        overwrote the first under some natural key, and it cannot see a row
        written twice.
        Unscoped, because `llm_calls` has no scope -- no `user_id` and no
        foreign key -- and because each integration test owns its own
        rolled-back transaction.
        """


class LLMCallRepositoryContract:
    """Subclasses supply a `repository` and a `ledger` fixture.

    Not an `ABC`, matching every other contract suite here: the fixtures are
    supplied by pytest rather than by inheritance, so `@abstractmethod` has
    nothing to attach to. What enforces the shape is that a subclass without
    the fixtures errors at collection.
    """

    async def test_a_call_that_worked_is_recorded_whole(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The control every other case needs, and four named defects in it.

        The wrong implementations this kills: a column dropped from the write
        (asserted by comparing the whole model, which is what `LLMCall` being
        `extra="forbid"` and 1:1 with its table buys); `tokens_out` filled
        from `tokens_in` or either filled from `latency_ms`, which are three
        adjacent integers and the classic wrong-slot write; a `purpose`
        written as a constant; and **`generation_id` lost**, which is the one
        that costs the most, because without it PRD 10's "cost per curated
        row" has nothing to join on and spend stops being attributable to any
        outcome at all.

        The three integers are pairwise distinct and the premise says so. A
        fixture that gave two of them one value would make the swap between
        those two invisible -- the same accident a UUIDv7 primary key produces
        for an `ORDER BY` key, one column over.
        """
        generation = new_id()
        call = llm_call(generation_id=generation)
        assert len({call.tokens_in, call.tokens_out, call.latency_ms}) == 3, (
            "the fixture must make the three integer columns tell each other apart"
        )
        assert call.id != call.generation_id, (
            "the fixture must make the two uuid columns tell each other apart"
        )

        await repository.record(call)

        stored = await ledger.get(call.id)
        # **Narrowest first, named columns next, whole-model compare last, and
        # the order is the whole difference between eleven live assertions and
        # eleven dead ones.** `LLMCall.__eq__` is total, so a leading
        # `stored == call` fails first for *any* difference -- `stored is None`
        # included -- and every line under it is unreachable. This suite
        # shipped that way: 24 lines across three cases that no defect could
        # ever reach, `generation_id` among them, which this case's docstring
        # calls the one that costs the most. Each of these now fails on its own
        # column name, and the `== call` at the end keeps its job, which is to
        # catch a column nobody thought to name.
        assert stored is not None, "the call was recorded and then could not be read back"
        assert stored.at == AT
        assert stored.model == MODEL
        assert stored.purpose is LLMPurpose.CURATION
        assert stored.tokens_in == TOKENS_IN
        assert stored.tokens_out == TOKENS_OUT
        assert stored.cost_usd == COST
        assert stored.latency_ms == LATENCY_MS
        assert stored.ok is True
        assert stored.error is None
        assert stored.generation_id == generation
        assert stored == call
        assert await ledger.count() == 1

    async def test_a_call_that_failed_is_a_row_with_its_error(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """**The whole point of the task.** The wrong implementation this
        kills is a `record()` that returns early on `ok = false` -- or one
        that drops `error`, or writes `ok` as a constant -- so the ledger
        holds only the calls that worked and understates spend by exactly the
        failures.

        **The failure modelled here is the one that is not an HTTP failure**,
        and it is chosen deliberately over a timeout. ADR-0028: a call that
        answered perfectly and validated to zero rows is `ok = false` with a
        reason, because that is the only signal separating a validator that
        ate the output from a model that had nothing to say -- and those two
        produce the identical empty screen. Such a call really did burn 1,200
        tokens and really was billed for them, which is why the premise below
        insists the fixture's failed call cost money: a `record()` that zeroed
        `cost_usd` on the failure path would otherwise be invisible, and it is
        the same understatement wearing a different column.
        """
        generation = new_id()
        call = llm_call(
            generation_id=generation,
            ok=False,
            error="the pool validator kept none of the four proposed rows",
        )
        assert call.cost_usd > 0, "a failed call that cost nothing cannot see a zeroed cost"

        await repository.record(call)

        stored = await ledger.get(call.id)
        # Narrowest first, whole-model compare last -- see the case above for
        # why the reverse makes every line between them unreachable.
        assert stored is not None, "the failed call was not recorded at all"
        assert stored.ok is False
        assert stored.error == "the pool validator kept none of the four proposed rows"
        assert stored.cost_usd == COST
        assert stored.tokens_in == TOKENS_IN
        assert stored.generation_id == generation
        assert stored == call
        assert await ledger.count() == 1, "the failed call is not in the ledger at all"

    async def test_a_failure_does_not_displace_the_success_before_it(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: a `record()` that replaces
        rather than appends -- a dict keyed on anything, or an `INSERT` grown
        an `ON CONFLICT DO UPDATE` to be "safe" against PRD 08's redelivery.

        Two calls, two generations, two rows. Both are read back whole rather
        than counted, because a store keyed on the *newest* row and one keyed
        on the *first* both leave a count of one and only reading says which
        survived.

        The premise is that the two generations differ, which is what makes
        this case the mirror of the one below rather than a duplicate of it.
        """
        worked = llm_call(generation_id=new_id())
        failed = llm_call(
            generation_id=new_id(), ok=False, error="upstream returned 502 after 118s"
        )
        assert worked.generation_id != failed.generation_id, (
            "this case is the differing-generation half; the sibling below is the shared one"
        )

        await repository.record(worked)
        await repository.record(failed)

        # **The two reads come first and the count last, which is the
        # ordering this case's third paragraph depends on.** Both wrong
        # implementations it names leave a count of one, so a leading
        # `count() == 2` fails before either read runs -- the case would be
        # red and the lines that say *which* row survived would never
        # execute. Measured by planting a `record()` that returns without
        # appending. The count still earns its place last, where it is the
        # only assertion that can see a third row written by mistake.
        assert await ledger.get(worked.id) == worked
        assert await ledger.get(failed.id) == failed
        assert await ledger.count() == 2

    async def test_two_calls_for_one_generation_are_two_rows(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: a write keyed on
        `generation_id` -- an `ON CONFLICT (generation_id)`, or a dict indexed
        by it -- which is the shape "one generation, one completion" invites.

        **This case exists because every other case in the suite mints a fresh
        `generation_id` per call**, so a store keyed on the generation is
        exactly as selective as one keyed on the row's own id and the two are
        indistinguishable everywhere else. That is the same trap M8 Task 9's
        sweep found one table over, where deleting `WHERE user_id` from a read
        survived all fourteen cases because every fixture gave each household
        its own generation.

        Two calls under one generation is not hypothetical: a retry after a
        malformed completion, or a second pass over a pool, spends twice for
        one outcome -- and PRD 10's "cost per curated row" is a `SUM(cost_usd)
        GROUP BY generation_id`, so a ledger that kept one of them halves the
        number the dashboard exists to report.
        """
        generation = new_id()
        first = llm_call(
            generation_id=generation,
            ok=False,
            error="the completion was not valid json",
            cost_usd=Decimal("0.0036"),
        )
        second = llm_call(generation_id=generation, cost_usd=Decimal("0.0087"))
        assert first.generation_id == second.generation_id, (
            "the fixture must make the two rows share a generation, or this case is its sibling"
        )
        assert first.id != second.id, (
            "the fixture must give the two rows distinct ids, or `get` cannot tell them apart"
        )

        await repository.record(first)
        await repository.record(second)

        # Reads first, count last, for the reason spelled out in the sibling
        # case above: a store keyed on `generation_id` leaves one row, so a
        # leading count would fail before the reads that say which one.
        assert await ledger.get(first.id) == first
        assert await ledger.get(second.id) == second
        assert await ledger.count() == 2

    async def test_a_call_belonging_to_no_generation_is_recorded(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: a write that requires a
        generation -- one that refuses `None`, or coalesces it to the row's
        own id, or hardcodes `purpose` to `curation` because that is the only
        value every other case uses.

        `LLMPurpose.QUERY_EXPANSION` produces no rows at all, so its ledger
        entry belongs to no generation. `QueryExpansionService` writes one per
        search that embeds, so on a deployment that curates and is searched
        these are the *majority* of the table, and `m08a`'s deferred
        `ix_llm_calls_generation_id` is declared partial for exactly that
        reason. A ledger that could not store them would drop the cheaper half
        of the spend and leave the expensive half looking like the whole.
        """
        call = llm_call(generation_id=None, purpose=LLMPurpose.QUERY_EXPANSION)
        assert call.purpose is not LLMPurpose.CURATION, (
            "the fixture must vary the purpose, or a constant write is invisible"
        )

        await repository.record(call)

        stored = await ledger.get(call.id)
        assert stored is not None, "the query-expansion call was not recorded at all"
        assert stored.generation_id is None
        assert stored.purpose is LLMPurpose.QUERY_EXPANSION
        assert stored == call
        assert await ledger.count() == 1

    @pytest.mark.parametrize("cost", MEASURED_COSTS, ids=str)
    async def test_a_cost_is_stored_exactly(
        self, repository: LLMCallRepository, ledger: LLMCallLedger, cost: Decimal
    ) -> None:
        """The wrong implementation this kills: a write that rounds or
        re-scales `cost_usd` on the way in.

        The values are `m08a`'s own measured table, which is where the
        column's scale came from. `0.00000002` is the one that matters:
        `$0.02/Mtok x 1 token` stores as `0.000000` at scale 6 and `0.0000` at
        scale 4, so a ledger that quantised on the way in would report a
        hosted model as **free** -- and it would do it for the cheapest calls
        while the expensive ones looked right, which makes the monthly total
        wrong by an amount nobody can see. Same failure class as this
        repository's `1 / (60 + rank)` integer division.

        `Decimal("0")` is in the list because both prices default to `0`: an
        operator who never priced their model produces that row on every
        single call, and it must read back as a zero rather than as a NULL.

        **Every value here is at or under scale 8, and that is a constraint
        the fake imposes on this case rather than Postgres.** The column
        rounds a ninth decimal place (bounded by 5e-9 USD per call, measured
        in `m08a`) and the fake does not, so a value with nine places would
        make a correct implementation fail on one arm -- see the fake's
        divergence list, where this is the entry pointing the other way.
        """
        call = llm_call(generation_id=new_id(), cost_usd=cost)

        await repository.record(call)

        stored = await ledger.get(call.id)
        assert stored is not None, "the call was recorded and then could not be read back"
        assert stored.cost_usd == cost

    async def test_recording_one_call_twice_is_a_conflict_rather_than_an_update(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: an upsert where an insert was
        asked for -- `ON CONFLICT (id) DO NOTHING`, which is what a reading of
        PRD 08's redelivery rule invites, or `DO UPDATE`.

        **Redelivery does not need it and is the reason it would be wrong.**
        A requeued `CURATE` job re-runs the whole generation and makes a
        *second* completion, which mints a fresh `LLMCall.id` and really did
        cost money a second time -- so the honest ledger holds two rows, and
        an insert already produces that. The only way to reach this conflict
        is a caller re-recording the identical object, which is a caller bug
        and not a state a retry clears. `TitleRepository.add` is the precedent:
        an insert, not an upsert, and a duplicate id raises.

        The constraint name is asserted on both arms, which is what makes the
        two agree rather than merely both raise -- `FakeTitleRepository`
        mirrors its real indexes name for name for the same reason.
        """
        call = llm_call(generation_id=new_id())
        await repository.record(call)

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(call)

        assert raised.value.constraint == "pk_llm_calls"
        assert await ledger.count() == 1
        assert await ledger.get(call.id) == call

    async def test_the_ledger_reads_back_every_attempted_call_in_the_window_including_the_failures(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """**The read, and the three wrong implementations it rules out**: a
        window that leaks the rows outside it, a window whose bounds are the
        wrong comparison, and a read that filters to the calls that *worked*.

        The last one is the expensive one and it is why the failed row here
        costs money. `02-data-model.md`'s `llm_calls` row: *"a ledger of
        successes alone understates spend by exactly the failures"*. A
        `WHERE ok` nobody wrote is the cheapest way to lose that property --
        it makes the read *look* right on every deployment whose calls all
        succeed, and wrong by exactly the amount an operator opened the
        dashboard to see. So the failed row is seeded with a real
        `cost_usd` and a real `tokens_in`, and its presence is asserted by
        name rather than only by the whole-list compare.

        **The two in-window rows differ on `generation_id` in the two states
        that column has**, which is not decoration: `ix_llm_calls_generation_id`
        is partial (`WHERE generation_id IS NOT NULL`) precisely because
        query-expansion rows carry `NULL`, and a read that dropped them would
        return the curation half of the spend while looking complete.

        **The bounds are asserted at the instant, not near it.** `FIRST_IN_
        WINDOW` *is* `WINDOW_START` and `AFTER_THE_WINDOW` *is* `WINDOW_END`,
        so narrowing `>=` to `>` loses the first row and widening `<` to `<=`
        gains the last one -- each a one-row diff on this case's own compare.
        The premises below state both equalities, because a later edit that
        moved either timestamp an hour clear of its bound would leave this
        case green against both mutants and nothing would say so.

        ⚠️ **What this case deliberately cannot see is a dropped `ORDER BY
        at`.** The four rows are minted in `at` order, so `new_id()`'s UUIDv7
        makes insertion order, id order and `at` order agree, and an
        unordered read passes here by accident.
        `test_the_window_is_returned_in_time_order_rather_than_in_the_order_
        it_was_written` is the case that separates them, and it carries the
        premise that makes it able to.
        """
        assert FIRST_IN_WINDOW == WINDOW_START, (
            "the first in-window row must sit exactly on the lower bound, or `>` survives"
        )
        assert AFTER_THE_WINDOW == WINDOW_END, (
            "the trailing row must sit exactly on the upper bound, or `<=` survives"
        )
        before = llm_call(generation_id=new_id(), at=BEFORE_THE_WINDOW)
        worked = llm_call(generation_id=new_id(), at=FIRST_IN_WINDOW)
        failed = llm_call(
            generation_id=None,
            at=SECOND_IN_WINDOW,
            purpose=LLMPurpose.QUERY_EXPANSION,
            ok=False,
            error="the pool validator kept none of the four proposed rows",
        )
        after = llm_call(generation_id=new_id(), at=AFTER_THE_WINDOW)
        seeded = [before, worked, failed, after]
        assert failed.cost_usd > 0, (
            "a failed call that cost nothing cannot see a read that filters on `ok`"
        )

        for call in seeded:
            await repository.record(call)

        # **The positive control, and it is `count()` rather than
        # `len(seeded)`.** `seeded` is a list this method just built, so its
        # length is four whether or not a single row reached the store -- and
        # against a `record()` that wrote nothing, an empty answer would then
        # be compared to an empty expectation and the case would pass. Only a
        # read of the store can say the fixture landed.
        assert await ledger.count() == 4, "the fixture did not write the four rows it seeded"

        found = await repository.list_since(WINDOW_START, until=WINDOW_END)

        assert list(found) == [worked, failed], (
            "the window must hold exactly the two calls inside it, in `at` order"
        )
        # Named separately from the compare above, because the whole-list
        # equality fails identically for a leaked row and for a dropped one,
        # and these two say which.
        assert failed in found, "the failed call is missing, so the read understates spend"
        assert before not in found and after not in found, (
            "the read returned a call from outside the window it was asked for"
        )

    async def test_the_window_is_returned_in_time_order_rather_than_in_the_order_it_was_written(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: a read with no `ORDER BY at`
        -- which Postgres answers in whatever order the chosen plan produces,
        and which the fake answers in insertion order.

        **The premise is the case.** `llm_calls.id` is a UUIDv7 minted by
        `new_id()`, so ids sort by *mint* time; every other case in this suite
        builds its rows in the order it wants them back, which makes
        `ORDER BY id`, `ORDER BY at` and no ordering at all give the identical
        answer. This repository has paid for that coincidence before -- it is
        the same accident `test_a_call_that_worked_is_recorded_whole` names
        for three adjacent integers, one column over. So these three rows are
        minted in the *reverse* of their `at` order, and
        `assert minted_order != sorted(minted_order)` refuses to let a later
        edit quietly restore the agreement.

        `at` is what the panels group on -- spend per day, and a trailing
        seven-day median -- so an answer in mint order is an answer in the
        order the *rows were inserted*, which for a redelivered job is not the
        order the calls happened in at all.
        """
        newest = llm_call(generation_id=new_id(), at=datetime(2026, 8, 5, 22, 0, tzinfo=UTC))
        middle = llm_call(generation_id=new_id(), at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
        oldest = llm_call(generation_id=new_id(), at=datetime(2026, 8, 5, 2, 0, tzinfo=UTC))
        in_at_order = [oldest, middle, newest]
        minted_order = [call.id for call in in_at_order]
        assert minted_order != sorted(minted_order), (
            "the fixture minted its ids in `at` order, so `ORDER BY id` and no ordering at "
            "all answer this case correctly and it proves nothing"
        )

        for call in (newest, middle, oldest):
            await repository.record(call)
        assert await ledger.count() == 3, "the fixture did not write the three rows it seeded"

        found = await repository.list_since(WINDOW_START, until=WINDOW_END)

        assert [call.at for call in found] == [oldest.at, middle.at, newest.at]
        assert list(found) == in_at_order

    async def test_a_window_with_no_end_runs_to_the_end_of_the_ledger(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """`until` is optional, and this is what its default means.

        The wrong implementation this kills: a read that supplies its own
        upper bound when the caller gave none -- `until or now()` is the
        tempting spelling, and it silently drops every row a clock skew or a
        `at` in the near future puts ahead of the server's idea of now. The
        ledger records *when the completion happened*, written by the caller
        rather than by a `server_default`, so "later than now" is a reachable
        state and not a corrupt one.

        🔴 **Which is why one row here is computed from the clock, and it is
        the only fixture in this suite that is.** Every other timestamp in this
        module is a literal, for `mutation-sweeps.md`'s reason: a fixture
        spelled as an offset from the bound it tests moves *with* the bound.
        This case tests the absence of a bound nobody wrote, and the bound it
        would have is `now()` -- so a literal cannot express it. Written with
        the module's own constants alone, `until or now()` keeps every seeded
        row (`AFTER_THE_WINDOW` is a date in the past) and the case is green
        against the exact implementation its first paragraph claims to kill.
        The premise below reads the row back through the ledger and asserts it
        really is ahead of the clock, so a later edit replacing it with a
        literal fails here rather than quietly disarming the case.

        The sibling case pins the closed window; this pins the open one, and
        the two together are what make `until` a parameter rather than a
        decoration. A read that ignored `until` entirely would pass here and
        fail the sibling; one that hardcoded an end would pass the sibling and
        fail here.
        """
        before = llm_call(generation_id=new_id(), at=BEFORE_THE_WINDOW)
        inside = llm_call(generation_id=new_id(), at=SECOND_IN_WINDOW)
        after = llm_call(generation_id=new_id(), at=AFTER_THE_WINDOW)
        ahead_of_the_clock = llm_call(generation_id=None, at=datetime.now(UTC) + A_CLEAR_DAY)

        for call in (before, inside, after, ahead_of_the_clock):
            await repository.record(call)
        assert await ledger.count() == 4, "the fixture did not write the four rows it seeded"
        stored = await ledger.get(ahead_of_the_clock.id)
        assert stored is not None and stored.at > datetime.now(UTC), (
            "the row meant to sit ahead of the server's clock does not, as stored, so "
            "`until or now()` survives this case"
        )

        found = await repository.list_since(WINDOW_START)

        assert list(found) == [inside, after, ahead_of_the_clock], (
            "an unbounded window must run past `WINDOW_END` and still start at `since`"
        )
        assert ahead_of_the_clock in found, (
            "the read supplied an upper bound the caller did not, dropping the call "
            "timestamped ahead of the server's clock"
        )
