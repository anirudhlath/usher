"""`FakeLLMCallRepository` against the shared `LLMCallRepository` contract.

No Docker, no database. See `tests/fakes/llm_call_repository.py` for the seven
places this half is more forgiving than
`tests/integration/test_llm_call_repository.py`'s -- the first of which is
that the fake stores the very `LLMCall` it was handed, so there is no column
mapping here to get wrong and almost every assertion in the contract is
structural on this arm and load-bearing on the other -- and for the one place
it is *stricter*, which is that it never rounds a cost to the column's scale.
"""

import uuid

import pytest

from tests.contract.llm_call_repository_contract import (
    LLMCallLedger,
    LLMCallRepositoryContract,
)
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from usher.domain.curation import LLMCall


class FakeLLMCallLedger(LLMCallLedger):
    """Reads the fake's own list.

    Bypasses nothing, because there is nothing to bypass: the port is
    append-only, so `record()` is the only writer either arm has and the
    ledger's whole job is to observe. It is an `LLMCallLedger` rather than a
    direct reach into `repository.calls` so that the *same* observation is
    made on both arms -- the contract asserts through this interface and
    cannot accidentally learn something only one implementation can answer.
    """

    def __init__(self, repository: FakeLLMCallRepository) -> None:
        self._repository = repository

    async def get(self, call_id: uuid.UUID) -> LLMCall | None:
        for call in self._repository.calls:
            if call.id == call_id:
                return call
        return None

    async def count(self) -> int:
        return len(self._repository.calls)


class TestFakeLLMCallRepository(LLMCallRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeLLMCallRepository:
        return FakeLLMCallRepository()

    @pytest.fixture
    def ledger(self, repository: FakeLLMCallRepository) -> FakeLLMCallLedger:
        # The *same* object the contract writes through. Two stores here would
        # make a correct implementation fail rather than a wrong one pass --
        # the arrangement `FakeCreditRepository` records for `titles`.
        return FakeLLMCallLedger(repository)
