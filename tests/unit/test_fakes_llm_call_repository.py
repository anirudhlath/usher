"""`FakeLLMCallRepository` against the shared `LLMCallRepository` contract."""

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

    Bypasses nothing, because there is nothing to bypass: `record()` is the
    only writer either arm has, and the port has no read at all. It is an
    `LLMCallLedger` rather than a direct reach into `repository.calls` so that
    the *same* observation is made on both arms -- the contract asserts through
    this interface and cannot accidentally learn something only one
    implementation can answer, nor satisfy a write case with a read carrying
    the mirrored defect.
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
