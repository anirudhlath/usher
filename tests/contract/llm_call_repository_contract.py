"""Behaviour every `LLMCallRepository` implementation must satisfy.

The port has **one method and no read**, which decides the shape of everything
below. `record()` is called on both the path where a generation worked and the
path where it did not -- a ledger holding only the successes understates spend
by exactly the failures, which are the rows an operator most wants to see --
and `ok` is the discriminator rather than "the HTTP call returned 200".

**A write is observed through an abstract `LLMCallLedger`, not through a read
method, and that is a decision rather than a convenience.** `llm_calls` has no
reader in `src/` and every reader PRD 10 names is a Grafana panel M10 builds;
`m08a` shipped the table with its primary key and no other index *on the
strength of that*, with the two future indexes written out as copy-pasteable
`CREATE INDEX` statements. A `list_since()` added here would be a method with
no caller -- `ix_titles_popularity` was an index nothing read and
`PushHealth.record_reconnect` was a method nothing called, and the second made
PRD 10's reconnect metric a permanent flat zero. So the suite reads the table
out of band, exactly as `CuratedRowSeeder` writes it out of band, and for the
mirrored reason: the port cannot express the observation the cases need.

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
from datetime import UTC, datetime
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

    Not a read method on `LLMCallRepository`. The port is append-only and has
    none by design -- see this module's docstring for why adding one would be
    the third instance of a surface built for a consumer that does not exist
    yet.

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

        What makes "append-only" assertable at all: `get` alone cannot tell a
        second `record()` that stored a second row from one that overwrote the
        first under some natural key, and it cannot see a row written twice.
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
        assert stored == call
        assert stored is not None
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
        assert stored == call
        assert stored is not None
        assert stored.ok is False
        assert stored.error == "the pool validator kept none of the four proposed rows"
        assert stored.cost_usd == COST
        assert stored.tokens_in == TOKENS_IN
        assert stored.generation_id == generation
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

        assert await ledger.count() == 2
        assert await ledger.get(worked.id) == worked
        assert await ledger.get(failed.id) == failed

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
        assert first.id != second.id

        await repository.record(first)
        await repository.record(second)

        assert await ledger.count() == 2
        assert await ledger.get(first.id) == first
        assert await ledger.get(second.id) == second

    async def test_a_call_belonging_to_no_generation_is_recorded(
        self, repository: LLMCallRepository, ledger: LLMCallLedger
    ) -> None:
        """The wrong implementation this kills: a write that requires a
        generation -- one that refuses `None`, or coalesces it to the row's
        own id, or hardcodes `purpose` to `curation` because that is the only
        value every other case uses.

        `LLMPurpose.QUERY_EXPANSION` produces no rows at all, so its ledger
        entry belongs to no generation; once Task 20 ships, these are the
        *majority* of the table, and `m08a`'s deferred
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
        assert stored == call
        assert stored is not None
        assert stored.generation_id is None
        assert stored.purpose is LLMPurpose.QUERY_EXPANSION
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
        assert stored is not None
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
