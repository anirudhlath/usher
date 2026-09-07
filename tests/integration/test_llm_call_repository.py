"""`PostgresLLMCallRepository` against the real database.

The shared contract runs here unchanged, and **this is the arm where nearly
all of it is load-bearing** rather than structural. The fake stores the very
`LLMCall` it was handed, so it has no column mapping to get wrong; this one
builds eleven parameters against eleven columns, which is where a dropped
`generation_id`, a `tokens_out` filled from `tokens_in` or a constant
`purpose` becomes expressible at all. `tests/fakes/llm_call_repository.py`
enumerates the six divergences and the one place the fake is stricter.

Plus the three things a list cannot express, each with a case of its own here:
a `NUMERIC(12, 8)` that refuses a number too large for it, the CHECK that
holds `ok` and `error` to each other, and the SAVEPOINT that lets a caller
keep using its session after the ledger refused a row -- which matters more on
this port than on any sibling, because `record()`'s caller is typically
already inside an exception handler with curated rows it still has to commit.

The ledger reads through a raw `SELECT *` into `LLMCall`, built from
`LLMCallRow`'s own column list. That is this schema's house shape and it is
what makes the comparison mechanically 1:1 with the table: a column added
without a field on the model raises here rather than being silently dropped.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.llm_call_repository_contract import (
    LLMCallLedger,
    LLMCallRepositoryContract,
    llm_call,
)
from tests.integration.conftest import A_DECISIVE_MARGIN, Analyze, index_suspended, total_cost
from usher.db.models.curation import LLMCallRow
from usher.db.repositories.llm_call import _LIST_SINCE_SQL, PostgresLLMCallRepository
from usher.domain.curation import LLMCall
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict, UsherPortError

_READ_ONE = "SELECT * FROM llm_calls WHERE id = CAST(:id AS uuid)"

#: How many ledger rows the plan assertion seeds, and **the number is the
#: assertion's premise rather than a convenience.**
#:
#: Measured 2026-09-07 on PostgreSQL 17.10 (`pgvector/pgvector:pg17`) at head
#: `m10c`, seeding one row an hour and asking for the same 24-hour window the
#: case uses, so **24 rows are selected whatever the table holds** and the only
#: thing varying down the table is the relation's size. Each row is `EXPLAIN`
#: of `_LIST_SINCE_SQL` with the index available against the same `EXPLAIN`
#: with it suspended, so the ratio compares this index with the *best
#: alternative* rather than with an assumption:
#:
#: | seeded rows | plan chosen | chosen | next best | ratio |
#: |---|---|---|---|---|
#: | 100   | `Seq Scan`   | 4.11 | 4.11   | **1.00** |
#: | 300   | `Index Scan` | 8.63 | 10.11  | **1.17** |
#: | 1,000 | `Index Scan` | 8.76 | 32.61  | 3.72 |
#: | 2,000 | `Index Scan` | 8.76 | 63.61  | 7.26 |
#: | 4,000 | `Index Scan` | 8.76 | 126.61 | 14.45 |
#:
#: 🔴 **The two bold rows are why this constant is 4,000 and not 300**,
#: and the danger at a small size is subtler than "the planner picks the wrong
#: plan". At 100 rows it picks `Seq Scan` and is *right* to -- the relation is
#: a handful of pages, so an index scan pays for heap fetches without saving a
#: read. But at 300 rows it already picks the index, so a case asserting only
#: the plan's *name* would be **green there** -- on a margin of 1.17, which is
#: a tie-break rather than a property of the schema. That is the shape of
#: issue #79's two CI failures, and it is what `A_DECISIVE_MARGIN` (2.0)
#: exists to fail on. 4,000 clears it seven times over.
#:
#: ⚠️ **The table above was measured against a *committed* relation**, and this
#: case's seed lives in a transaction that is rolled back -- one more page, so
#: the numbers here are near the table's rather than equal to them. Planted at
#: 300 the same day, this case reads 8.63 against 11.11, **1.29x**: a different
#: number and the identical verdict, which is why the assertion is on the
#: margin and not on either figure.
_SEEDED_LEDGER_ROWS = 4000

#: One row an hour, back from the window, so the window's selectivity is a
#: property of the fixture rather than of a clock.
_LEDGER_ORIGIN = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)


class PostgresLLMCallLedger(LLMCallLedger):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, call_id: uuid.UUID) -> LLMCall | None:
        result = await self._session.execute(text(_READ_ONE), {"id": call_id})
        row = result.one_or_none()
        if row is None:
            return None
        columns = [column.name for column in LLMCallRow.__table__.columns]
        return LLMCall.model_validate({name: row._mapping[name] for name in columns})

    async def count(self) -> int:
        found = await self._session.execute(text("SELECT count(*) FROM llm_calls"))
        return int(found.scalar_one())


class TestPostgresLLMCallRepository(LLMCallRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresLLMCallRepository:
        return PostgresLLMCallRepository(session)

    @pytest.fixture
    def ledger(self, session: AsyncSession) -> PostgresLLMCallLedger:
        # The same session, so what the contract writes and what it reads back
        # are in the transaction this test owns.
        return PostgresLLMCallLedger(session)

    async def test_a_cost_the_column_cannot_hold_is_a_port_error(
        self, repository: PostgresLLMCallRepository, ledger: PostgresLLMCallLedger
    ) -> None:
        """**The case the whole error contract rests on**, and Postgres-only
        because a Python `Decimal` has no ceiling to hit.

        `cost_usd` is `NUMERIC(12, 8)`, so four integer digits: a single call
        above `$9,999.99999999` raises `numeric field overflow`. The
        misconfiguration that precision exists to catch is a price scaled *up*
        by a million on the way in -- `$36,000` on one 12,000-token call --
        and `usher.db.models.curation`'s module docstring holds the one copy
        of that argument.

        **It is reachable from a validly constructed `LLMCall`**, which is
        what separates it from every other refusal on this table: the model
        bounds `cost_usd` with `ge=0` and no upper limit, so no `model_
        construct` is needed here and a service doing everything right can
        still produce this row. That is why the translation exists at all --
        the primary key alone would not have justified it, since a fresh
        UUIDv7 makes a duplicate nearly unreachable.

        **And the exception it must catch is not the obvious one** -- which is
        the whole reason this case is worth its round trip.
        `usher.db.repositories._errors.ROW_REFUSED_SQLSTATE_CLASSES` holds the
        measurement and the two exception types it is *not*; what matters here
        is that an implementation catching `IntegrityError` alone, which is
        what most sibling repositories catch and what this one caught before
        the measurement, lets a raw SQLAlchemy exception cross the port
        boundary. The only way a caller could then handle it is to import
        sqlalchemy itself, which is the one thing ADR-0009 says must never
        happen.

        There is no constraint to name, so `constraint` is `None`: this is the
        column's declared precision refusing a value, not a named constraint
        firing.
        """
        priced_a_million_times_over = llm_call(
            generation_id=new_id(), cost_usd=Decimal("36000.00000000")
        )

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(priced_a_million_times_over)

        assert raised.value.constraint is None
        assert await ledger.count() == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"ok": False, "error": None}, id="failed-with-no-reason"),
            pytest.param({"ok": False, "error": ""}, id="failed-with-an-empty-reason"),
            pytest.param({"ok": True, "error": "but it worked"}, id="succeeded-carrying-an-error"),
        ],
    )
    async def test_a_row_whose_ok_and_error_disagree_is_refused_by_the_table(
        self,
        repository: PostgresLLMCallRepository,
        ledger: PostgresLLMCallLedger,
        overrides: dict[str, object],
    ) -> None:
        """`ck_llm_calls_ok_error_agree`, reached through the repository
        rather than through raw SQL.

        Constructed with `model_construct`, because
        `LLMCall._ok_and_error_must_agree` refuses all three first -- which is
        exactly why the CHECK exists and why that validator is a
        `model_validator(mode="after")` rather than a `model_post_init` hook:
        `model_construct` skips a validator and *runs* a post-init hook, so
        under the other spelling this case would be unwritable and the CHECK
        would be a constraint nothing had ever proved was real. Its own
        docstring says so.

        The three shapes are not one case repeated. A failed call with no
        reason is a row an operator cannot act on; a failed call whose reason
        is the empty string is the same row wearing a value, and it is the one
        `str(exc)` produces for an exception raised with no arguments, which
        is the reachable spelling this port's docstring warns Tasks 11-13
        about; and a successful call carrying an error reads as a *failure* in
        every `WHERE error IS NOT NULL` anybody will ever write against this
        ledger. The `AND error <> ''` half of the constraint is what the
        second one needs, and without it that row stores.

        `tests/integration/test_curation_schema.py` owns the constraint
        itself; this owns the translation, which is the half a caller sees.
        """
        valid = llm_call(generation_id=new_id())
        refused = valid.model_construct(**{**valid.model_dump(), **overrides})

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(refused)

        assert raised.value.constraint == "ck_llm_calls_ok_error_agree"
        assert await ledger.count() == 0

    async def test_a_refused_call_leaves_the_earlier_rows_and_the_session_usable(
        self, repository: PostgresLLMCallRepository, ledger: PostgresLLMCallLedger
    ) -> None:
        """**The SAVEPOINT**, and it buys more on this port than on its
        siblings.

        The wrong implementation this kills: a `record()` with no nested
        transaction. The refused `INSERT` aborts the caller's transaction, so
        the very next statement on that session raises `PendingRollbackError`
        with the failure attributed to whatever ran next -- and `record()`'s
        caller is, by construction, a service already inside an exception
        handler that still has curated rows to commit. A ledger write that
        poisons the session turns a failed *call* into a lost *generation*.

        Three assertions, in the order the damage would arrive: the earlier
        row is still there (the SAVEPOINT rolled back to a point after it),
        the refused row is not, and a subsequent unrelated `record()` on the
        same session both succeeds and is visible. The last one is the only
        one that can see a missing SAVEPOINT; the first two are what a
        SAVEPOINT scoped too widely would break.
        """
        earlier = llm_call(generation_id=new_id())
        await repository.record(earlier)

        with pytest.raises(RepositoryConflict):
            await repository.record(llm_call(generation_id=new_id(), cost_usd=Decimal("36000")))

        assert await ledger.get(earlier.id) == earlier
        assert await ledger.count() == 1

        later = llm_call(generation_id=new_id(), cost_usd=Decimal("0.0036"))
        await repository.record(later)
        assert await ledger.get(later.id) == later
        assert await ledger.count() == 2

    async def test_a_failure_that_is_not_the_rows_fault_is_not_reported_as_one(
        self,
        repository: PostgresLLMCallRepository,
        ledger: PostgresLLMCallLedger,
        session: AsyncSession,
    ) -> None:
        """The other side of the error contract, and the case that makes the
        SQLSTATE filter load-bearing rather than decorative.

        The wrong implementation this kills: an `except DBAPIError` that
        translates **everything** into `RepositoryConflict`. Catching the whole
        class is what `test_a_cost_the_column_cannot_hold_is_a_port_error`
        forces, and the naive way to satisfy that case is to translate the lot.
        Then a dropped connection, a statement timeout or a schema that is not
        there arrives at `CurationService` as "this row is not storable", which
        is the one failure kind a caller must be able to tell apart: a row that
        is wrong is a bug in the generation, and a transport that is gone is
        something a retry fixes. A redundant-looking predicate is a coverage
        question, not a style question.

        SQLSTATE `42P01` (undefined table) is class 42, so it is outside the
        `22`/`23` classes `ROW_REFUSED_SQLSTATE_CLASSES` names, and it is
        deterministic where a timeout would not be. The rename is blunt on
        purpose: what is exercised is the *class* of failure, not a plausible
        operational story.

        **What the rename costs, stated because a test that mutates shared
        state owes it.** `postgres_url` is session-scoped and the schema is
        built once; this is the only case in the suite that changes it.
        Measured: `ALTER TABLE ... RENAME` takes an `AccessExclusiveLock` on
        `llm_calls` and holds it for the rest of the transaction, and a
        concurrent `SELECT count(*)` from a second session blocks until it
        times out. Two consequences:

        - **Safety comes from DDL being transactional in PostgreSQL, not from
          the `finally`.** The explicit rename back exists so the assertions
          below can read the table; the reason a crash between the two cannot
          leave the schema renamed is that the enclosing per-test transaction
          is rolled back and the DDL goes with it. That is the load-bearing
          fact and it was previously only implied.
        - **This case is the exception to `tests/integration/conftest.py`'s
          xdist note**, which grounds its claim on isolation never coming from
          resetting something shared. That holds for every other test here and
          not for this one: run in parallel against one container, a worker
          touching `llm_calls` while this lock is held would block rather than
          fail, which is slow and confusing rather than wrong. Worth knowing
          before anyone adopts `pytest-xdist`.

        **The failure is captured by hand rather than with
        `pytest.raises(DBAPIError)`, and that is the whole difference between
        this case discriminating and merely failing.** Under the mutation this
        names, `record()` raises `RepositoryConflict` — which is a
        `UsherPortError` and therefore *not* a `DBAPIError` — so
        `pytest.raises` would decline it, let it propagate, and fail the case
        before reaching a single assertion. The case would still be red, but
        the line claiming to tell the two apart would never run, which is the
        defect `35176e0` and `4608f3b` are both about. Captured into a
        variable, the discriminating assertion is the one that fails and it
        names what happened. `pytest.raises(Exception)` would have the same
        property and is refused for two reasons: ruff's `B017` forbids it
        without a `match=`, and a `match=` on a driver's message text is
        exactly the dialect- and locale-dependent parsing that
        `constraint_name` exists to avoid.
        """
        raised: Exception | None = None
        await session.execute(text("ALTER TABLE llm_calls RENAME TO llm_calls_moved_away"))
        try:
            await repository.record(llm_call(generation_id=new_id()))
        # Deliberately wide: which exception this is *is* the assertion below.
        except Exception as exc:
            raised = exc
        finally:
            await session.execute(text("ALTER TABLE llm_calls_moved_away RENAME TO llm_calls"))

        assert raised is not None, "a write against a table that is not there did not raise"
        assert not isinstance(raised, UsherPortError), (
            f"an undefined table reached the caller as {type(raised).__name__}, which tells a "
            "service the row was wrong when the schema is what is missing"
        )
        assert isinstance(raised, DBAPIError)
        cause = getattr(raised.orig, "__cause__", None)
        assert getattr(cause, "sqlstate", None) == "42P01"
        assert await ledger.count() == 0

    async def test_the_cost_lands_in_the_numeric_column_at_its_declared_scale(
        self, repository: PostgresLLMCallRepository, session: AsyncSession
    ) -> None:
        """The contract's `test_a_cost_is_stored_exactly` compares two
        `Decimal`s and this reads the column's own rendering, which is a
        different claim: `0.00000002` and `0.00000002000` compare equal, so
        equality alone cannot say the value landed at scale 8 rather than
        being carried by something wider that happened to agree.

        The wrong implementation this kills: a write routed through a column
        or a cast this table does not have. Measured while writing this task,
        and recorded because it is the reason the sibling case is not enough
        on its own -- and also because it bounds what *this* case can claim:
        handing the driver a Python `float` for this parameter is **accepted
        and value-preserving** at this scale (`0.0087` stores `0.00870000`,
        `2e-08` stores `0.00000002`, and even `1/3` stores `0.33333333`), so
        neither case can see a `float()` on the way in. What both see is a
        *re-scaling*: `Decimal("0.00000002").quantize(Decimal("0.0001"))`
        stores `0.00000000`, a real call reported as free.
        """
        call = llm_call(generation_id=new_id(), cost_usd=Decimal("0.00000002"))

        await repository.record(call)

        rendered = await session.execute(
            text("SELECT cost_usd::text FROM llm_calls WHERE id = CAST(:id AS uuid)"),
            {"id": call.id},
        )
        assert rendered.scalar_one() == "0.00000002"

    async def test_the_windowed_read_is_served_by_the_time_index(
        self,
        repository: PostgresLLMCallRepository,
        session: AsyncSession,
        analyze: Analyze,
    ) -> None:
        """**`ix_llm_calls_at` earns its keep**, measured on the statement the
        repository actually issues rather than on a transcription of it --
        `_LIST_SINCE_SQL` is imported, not retyped.

        `m08a` wrote this index out by name beside this exact predicate
        (*"dashboard 5's 'LLM spend per day and month' and the cost-anomaly
        alert, both `WHERE at >= :since`"*) and refused to ship it, because
        *"an index nothing reads is `ix_titles_popularity` again"*. `m10c`
        shipped it anyway, one revision ahead of any reader and saying so.
        This case is the other end of that: the reader exists, and the planner
        agrees the index is what serves it.

        🔴 **The seeded size is the assertion's premise**, and
        `_SEEDED_LEDGER_ROWS` carries the measured table that picked it. The
        short version: at 100 rows the planner chooses a `Seq Scan` and is
        *right* to; at 300 it chooses this index on a margin of **1.17**,
        which is a tie-break wearing a measurement's clothes; at 4,000 the
        margin is **14.45**. A case asserting only the plan's name would be
        green at 300 and would be reporting tie-breaking order -- the shape of
        issue #79's two CI failures. So this seeds one row an hour and asks for
        a **24-hour** window: 24 rows of 4,000, **0.6%**, inside the ~1%
        selectivity where a btree beats a scan, and the same arithmetic
        `m10c`'s docstring uses for `ix_search_queries_at`.

        **The margin is asserted rather than the winner's name alone.**
        `index_suspended` hides `ix_llm_calls_at` and re-plans, so what is
        compared is this index against the *best alternative* rather than
        against an assumption -- `A_DECISIVE_MARGIN`'s docstring records the
        two CI failures that taught this suite the difference.

        **The runner-up is a `Sort` over a `Seq Scan`, which is worth
        seeing**: with the index suspended Postgres has to sort for the
        `ORDER BY at` it otherwise gets free from the index's own order. One
        index serves both halves of this statement, which is why deleting the
        `ORDER BY` would not even buy a cheaper plan.

        **And the read is executed through the port afterwards.** A plan
        measured against a statement nobody runs is a plan for a statement that
        may not answer correctly; `test_the_newest_generation_costs_one_pass_
        over_the_table` one module over takes the same care for the same
        reason. The 24 rows are asserted, so a window that planned beautifully
        and returned the wrong slice fails here rather than passing.
        """
        window_end = _LEDGER_ORIGIN
        window_start = _LEDGER_ORIGIN - timedelta(hours=24)
        await session.execute(
            text(
                "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out,"
                " cost_usd, latency_ms, ok, error, generation_id) "
                "SELECT gen_random_uuid(),"
                "       CAST(:origin AS timestamptz) - make_interval(hours => hour),"
                "       'fake:test-model', 'curation', 1200, 340, 0.0087, 4310,"
                "       true, NULL, gen_random_uuid() "
                "FROM generate_series(0, :rows - 1) AS hour"
            ),
            {"origin": _LEDGER_ORIGIN, "rows": _SEEDED_LEDGER_ROWS},
        )
        # Without statistics the planner sizes `llm_calls` off an empty
        # `pg_class`, every candidate costs the same to four significant
        # figures, and which one it names is decided by nothing this test
        # controls. `analyze`'s own docstring records a case that failed 10
        # runs of 10 for exactly that.
        await analyze("llm_calls")
        seeded = await session.execute(text("SELECT count(*) FROM llm_calls"))
        assert seeded.scalar_one() == _SEEDED_LEDGER_ROWS, (
            "the premise: the plan below is asserted at a size where the index can win"
        )

        parameters = {"since": window_start, "until": window_end}
        plan = await _explain(session, parameters)
        assert "Index Scan using ix_llm_calls_at" in plan, plan
        # **And the plan's *property* beside its artefact's name**, which is the
        # order `db-and-sql.md` puts them in: a name survives a later migration
        # adding a second index over `at`, and it also survives both bounds
        # arriving as a post-scan `Filter` on a scan the index only positioned
        # by its lower half. What the index is for is *bounding* the read, so
        # what is asserted is that both comparisons became the `Index Cond` and
        # that nothing was left over to filter.
        assert "Index Cond: ((at >= " in plan and "Filter:" not in plan, (
            f"the window did not become this index's condition, so the scan is bounded by "
            f"less than the caller asked for:\n{plan}"
        )

        async with index_suspended(session, "ix_llm_calls_at"):
            runner_up = await _explain(session, parameters)
        assert "ix_llm_calls_at" not in runner_up, (
            f"the index was not actually suspended, so the comparison below measures "
            f"nothing:\n{runner_up}"
        )
        assert total_cost(runner_up) > total_cost(plan) * A_DECISIVE_MARGIN, (
            f"the time index wins by too little for this to be a property of the schema "
            f"rather than of tie-breaking order -- the fixture is back below the scale at "
            f"which the planner can tell the candidates apart, at "
            f"{_SEEDED_LEDGER_ROWS} rows.\n"
            f"chosen {total_cost(plan)}:\n{plan}\nnext best {total_cost(runner_up)}:\n{runner_up}"
        )

        found = await repository.list_since(window_start, until=window_end)
        assert len(found) == 24, (
            "the window that planned well returned the wrong slice, so the plan above was "
            "measured against a statement that does not answer the question"
        )
        assert [call.at for call in found] == sorted(call.at for call in found)


async def _explain(session: AsyncSession, parameters: Mapping[str, object]) -> str:
    """`EXPLAIN` in **text** format, because `total_cost` reads the root
    node's `(cost=start..total ` off the first line.

    `EXPLAIN` without `ANALYZE`: what is asserted is the plan the planner
    *chose*, and executing it would add runtime to a comparison whose whole
    point is the estimate the two candidates were ranked on.
    """
    result = await session.execute(text("EXPLAIN " + _LIST_SINCE_SQL), parameters)
    return "\n".join(str(row[0]) for row in result)
