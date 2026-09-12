"""The LLM cost ledger: a write on every attempted completion."""

from abc import ABC, abstractmethod

from usher.domain.curation import LLMCall

__all__ = [
    "LLMCallRepository",
]


class LLMCallRepository(ABC):
    """`llm_calls` -- PRD 10's cost ledger, one row per *attempted* completion."""

    @abstractmethod
    async def record(self, call: LLMCall) -> None:
        """Append one *attempted* completion to the ledger, whether or not it worked -- and
        "worked" is not "got an answer".
        """
