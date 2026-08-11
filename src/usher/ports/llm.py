"""Port for large language model completions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Re-exported rather than declared. `LLMPurpose` moved to `usher.domain` in
# M8 because `LLMCall` -- a domain model -- has to type its `purpose` column,
# and `usher.domain` may not import `usher.ports`. Every caller still spells
# it `from usher.ports.llm import LLMPurpose`, which is where a reader of this
# port looks for it.
from usher.domain.curation import LLMPurpose


@dataclass(frozen=True)
class LLMUsage:
    """Token counts and cost for a single completion."""

    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int


class LLMClient(ABC):
    """Provider-agnostic completion interface."""

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: LLMPurpose,
    ) -> tuple[dict[str, Any], LLMUsage]:
        """Return a JSON object conforming to `schema`, plus usage for cost
        accounting. `purpose` is recorded against the call."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""


__all__ = ["LLMClient", "LLMPurpose", "LLMUsage"]
