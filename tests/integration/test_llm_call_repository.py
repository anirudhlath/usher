"""`PostgresLLMCallRepository` against the real database."""

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
        """**The case the whole error contract rests on**.

        and Postgres-only because a Python `Decimal` has no ceiling to hit.
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
        """`ck_llm_calls_ok_error_agree`.

        reached through the repository rather than through raw SQL.
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
        """**The SAVEPOINT**, and it buys more on this port than on its siblings.

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
        """The other side of the error contract.

        and the case that makes the SQLSTATE filter load-bearing rather than decorative.
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
        """The contract's `test_a_cost_is_stored_exactly` compares two `Decimal`s and this reads.

        the column's own rendering, which is a different claim: `0.00000002` and
        `0.00000002000` compare equal, so equality alone cannot say the value landed at
        scale 8 rather than being carried by something wider that happened to agree.

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
