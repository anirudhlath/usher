"""A scripted `LLMClient`, for every test that is about what the *service* does with a
completion rather than about how one is fetched.
"""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from usher.ports.llm import LLMClient, LLMPurpose, LLMUsage

DEFAULT_MODEL = "fake/scripted-1"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One `complete_json`, kept so a case can assert on what was sent.

    The prompt is kept in full rather than truncated: several cases assert
    that the household's watch history reached the prompt, and one asserts
    that a credential did *not*.
    """

    prompt: str
    schema: dict[str, Any]
    purpose: LLMPurpose


def usage(
    *,
    model: str = DEFAULT_MODEL,
    tokens_in: int = 1_000,
    tokens_out: int = 200,
    cost_usd: Decimal = Decimal("0"),
    latency_ms: int = 5,
) -> LLMUsage:
    """An `LLMUsage` with plausible defaults, so a case that is not about
    usage does not have to name five fields."""
    return LLMUsage(
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


@dataclass
class FakeLLMClient(LLMClient):
    """Hands back scripted responses in order; repeats the last one forever.

    Repeating rather than raising on exhaustion is deliberate: the contract
    suite calls `complete_json` once per case over a shared fixture, and a
    fake that ran out would make every case after the first fail for a reason
    that has nothing to do with what it is testing. A case that cares how many
    calls were made asserts on `calls`.

    A scripted entry that is an exception is **raised** rather than returned,
    which is how a case drives `CurationService`'s failure path without
    needing a transport.
    """

    responses: deque[dict[str, Any] | BaseException] = field(default_factory=deque)
    usages: deque[LLMUsage] = field(default_factory=deque)
    calls: list[RecordedCall] = field(default_factory=list)
    closed: int = 0

    @classmethod
    def returning(
        cls, *bodies: dict[str, Any] | BaseException, usages: Sequence[LLMUsage] = ()
    ) -> "FakeLLMClient":
        return cls(responses=deque(bodies), usages=deque(usages))

    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: LLMPurpose,
    ) -> tuple[dict[str, Any], LLMUsage]:
        self.calls.append(RecordedCall(prompt=prompt, schema=dict(schema), purpose=purpose))
        if not self.responses:
            body: dict[str, Any] | BaseException = {"ok": True}
        elif len(self.responses) == 1:
            body = self.responses[0]
        else:
            body = self.responses.popleft()
        if isinstance(body, BaseException):
            raise body
        if not self.usages:
            reported = usage()
        elif len(self.usages) == 1:
            reported = self.usages[0]
        else:
            reported = self.usages.popleft()
        return body, reported

    async def aclose(self) -> None:
        self.closed += 1
