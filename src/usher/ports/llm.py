"""Port for large language model completions."""

from abc import ABC, abstractmethod
from typing import Any


class LLMClient(ABC):
    """Provider-agnostic completion interface."""

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: str,
    ) -> tuple[dict[str, Any], "LLMUsage"]:
        """Return a JSON object conforming to `schema`, plus usage for cost
        accounting. `purpose` is recorded against the call."""


class LLMUsage:
    """Token counts and cost for a single completion."""

    def __init__(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        self.model = model
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
