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
from usher.db.models.curation import LLMCallRow
from usher.db.repositories.llm_call import PostgresLLMCallRepository
from usher.domain.curation import LLMCall
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict, UsherPortError

_READ_ONE = "SELECT * FROM llm_calls WHERE id = CAST(:id AS uuid)"


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

        **And the exception it must catch is not the obvious one.** Measured
        on `pgvector/pgvector:pg17`: this arrives as a bare `sqlalchemy.exc.
        DBAPIError`, **not** an `IntegrityError` and **not** even a
        `DataError` -- SQLAlchemy's asyncpg dialect does not classify
        `asyncpg.exceptions.NumericValueOutOfRangeError` (SQLSTATE `22003`)
        into either. An implementation catching `IntegrityError` alone -- which
        is what most sibling repositories in this package catch, and what this
        one caught before the measurement -- lets a raw SQLAlchemy exception
        cross the port boundary, the one thing ADR-0009 says must never happen,
        since the only way a caller could handle it is to import sqlalchemy
        itself. `PostgresCuratedRowRepository` has since been measured to need
        the same widening for `curated_rows."position"`, so the shared filter
        lives in `usher.db.repositories._errors`.

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
        translates **everything** into `RepositoryConflict`. Catching the
        whole class is what `test_a_cost_the_column_cannot_hold_is_a_port_
        error` forces -- `numeric field overflow` is not an `IntegrityError`
        and not a `DataError` -- and the naive way to satisfy that case is to
        translate the lot. Then a dropped connection, a statement timeout or a
        schema that is not there arrives at `CurationService` as "this row is
        not storable", which is the one failure kind a caller must be able to
        tell apart: a row that is wrong is a bug in the generation, and a
        transport that is gone is something a retry fixes. A redundant-looking
        predicate is a coverage question, not a style question.

        SQLSTATE `42P01` (undefined table) is class 42, so it is outside the
        `22`/`23` classes that mean "this row is not storable as given", and
        it is deterministic where a timeout would not be. The rename is
        blunt on purpose: what is being exercised is the *class* of failure,
        not a plausible operational story, and it is reverted before the
        assertions so the ledger can be read back. The whole transaction is
        rolled back afterwards regardless.

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
