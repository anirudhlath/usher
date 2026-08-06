"""The behavioural contract every `LLMClient` implementation must satisfy.

Run against `FakeLLMClient` (`tests/unit/`) and against
`OpenAICompatibleClient` over `httpx.MockTransport` (`tests/unit/`, because
`MockTransport` needs no container). A third subclass driving a **live**
endpoint lives in `tests/integration/` and skips itself unless one is
configured -- a contract suite that passes because nothing ran is the
`sitecustomize.py` trap, so that subclass asserts it actually reached
something before it believes its own result.

**What this suite deliberately does not assert.** Anything about the
*content* of a completion. A contract case that expected particular rows
would be a test of a model, and every implementation here is free to be
driven by a script, a fixture or a real endpoint. What is shared is the
shape of the answer, the shape of the failure, and the fact that usage comes
back at all -- which is the half `usher.db.repositories.curation` writes to a
ledger and which an implementation returning only the parsed object would
pass every content assertion without.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import pytest

from usher.ports.llm import LLMClient, LLMPurpose, LLMUsage

# A schema small enough to read and specific enough that a client which
# ignored it entirely would be visible in a live run.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class LLMClientContract(ABC):
    """Subclass and implement `client()`."""

    @abstractmethod
    def client(self) -> LLMClient:
        """A client whose next `complete_json` succeeds and returns an
        object with an `ok` key."""

    async def test_a_completion_returns_the_object_and_its_usage(self) -> None:
        """Kills an implementation that returns only the parsed object.

        `llm_calls` is a cost ledger and its writer is `CurationService`,
        which has no other source for the token counts. A client that
        answered with the object alone would pass every assertion anybody
        writes about rows and leave PRD 10's dashboard 5 permanently empty.
        """
        client = self.client()
        body, usage = await client.complete_json(
            "reply with ok true", SCHEMA, purpose=LLMPurpose.CURATION
        )
        assert isinstance(body, dict)
        assert "ok" in body
        assert isinstance(usage, LLMUsage)

    async def test_usage_names_a_model_and_carries_non_negative_counts(self) -> None:
        """Kills a usage object assembled from defaults.

        `model` is what PRD 10's dashboard groups spend by, so an empty
        string there collapses every model into one bar.
        """
        client = self.client()
        _body, usage = await client.complete_json(
            "reply with ok true", SCHEMA, purpose=LLMPurpose.CURATION
        )
        assert usage.model
        assert usage.tokens_in >= 0
        assert usage.tokens_out >= 0
        assert usage.latency_ms >= 0

    async def test_cost_is_a_decimal_and_never_a_float(self) -> None:
        """Kills `cost_usd = tokens * price` computed in binary floating
        point.

        Pinned on the port too (`test_llm_usage_cost_is_decimal_not_float`),
        and again here because that case constructs an `LLMUsage` by hand and
        this one takes whatever the implementation built.
        """
        client = self.client()
        _body, usage = await client.complete_json(
            "reply with ok true", SCHEMA, purpose=LLMPurpose.CURATION
        )
        assert isinstance(usage.cost_usd, Decimal)
        assert usage.cost_usd >= 0

    @pytest.mark.parametrize("purpose", list(LLMPurpose))
    async def test_every_purpose_in_the_vocabulary_is_accepted(self, purpose: LLMPurpose) -> None:
        """Kills an implementation that branches on `purpose` and handles one
        member.

        `LLMPurpose` is closed precisely so it stays a usable telemetry
        dimension; parametrising over the enum means a member added without a
        call site fails here rather than at the first request that uses it.
        """
        client = self.client()
        _body, usage = await client.complete_json("reply with ok true", SCHEMA, purpose=purpose)
        assert usage.model

    async def test_aclose_is_idempotent(self) -> None:
        """Kills an `aclose` that raises on a second call.

        The composition root closes what it built, and `usher curate` closes
        in a `finally` that can run after an error path has already closed.
        """
        client = self.client()
        await client.aclose()
        await client.aclose()
