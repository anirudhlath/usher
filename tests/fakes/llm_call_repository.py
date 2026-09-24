"""In-memory `LLMCallRepository`."""

from usher.domain.curation import LLMCall
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import LLMCallRepository


class FakeLLMCallRepository(LLMCallRepository):
    def __init__(self) -> None:
        #: Every recorded call, in the order it was recorded -- the table,
        #: not a screen. The port has no read, so nothing here sorts.
        self.calls: list[LLMCall] = []

    async def record(self, call: LLMCall) -> None:
        # `pk_llm_calls`, modelled rather than diverged -- see the module
        # docstring. The check is before the append, so a refused call leaves
        # the ledger exactly as it was, which is also what the real one's
        # SAVEPOINT buys on the arm that has a transaction.
        if any(stored.id == call.id for stored in self.calls):
            raise RepositoryConflict(
                f"llm call {call.id} is already in the ledger", constraint="pk_llm_calls"
            )
        # Append, never replace. A store keyed on `generation_id` would look
        # identical against any fixture that mints one generation per call,
        # which is exactly what `test_two_calls_for_one_generation_are_two_
        # rows` exists to make false.
        self.calls.append(call)
