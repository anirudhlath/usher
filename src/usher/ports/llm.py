"""Port for large language model completions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


class LLMPurpose(StrEnum):
    """`llm_calls.purpose` (PRD 10) — a closed vocabulary so it stays a
    usable telemetry dimension instead of a cardinality footgun. PRD 10's
    own text marks this open-ended ("curation | query_expansion | …"): a
    new call site adds a member here and to PRD 10 in the same change,
    never a free-form string."""

    CURATION = "curation"
    QUERY_EXPANSION = "query_expansion"


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
