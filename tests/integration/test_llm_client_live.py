"""`LLMClientContract` against a real endpoint, when one is configured."""

import os

import pytest

from tests.contract.llm_client_contract import LLMClientContract
from usher.adapters.llm.openai_compatible import OpenAICompatibleClient
from usher.ports.llm import LLMClient, LLMPurpose

_BASE_URL = os.environ.get("USHER_TEST_LLM_BASE_URL")
_API_KEY = os.environ.get("USHER_TEST_LLM_API_KEY")
_MODEL = os.environ.get("USHER_TEST_LLM_MODEL", "gpt-4o-mini")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _BASE_URL,
        reason="set USHER_TEST_LLM_BASE_URL to run the live LLM contract",
    ),
]


def _client() -> OpenAICompatibleClient:
    from pydantic import SecretStr

    return OpenAICompatibleClient(
        model=_MODEL,
        base_url=_BASE_URL or "",
        api_key=SecretStr(_API_KEY) if _API_KEY else None,
        max_output_tokens=256,
    )


class TestLiveLLMClient(LLMClientContract):
    def client(self) -> LLMClient:
        return _client()


async def test_the_endpoint_was_really_reached() -> None:
    """The control, without which every case above could be satisfied by a
    client that answered from a cache.

    A non-zero `tokens_in` cannot be produced by a misconfigured base URL, by
    a fake, or by a skipped run — and a skipped run does not reach here at
    all, which is the point of the module-level `skipif` over a per-case one.
    """
    client = _client()
    try:
        body, usage = await client.complete_json(
            'Reply with {"ok": true} and nothing else.',
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            purpose=LLMPurpose.CURATION,
        )
    finally:
        await client.aclose()
    assert body.get("ok") is True
    assert usage.tokens_in > 0
    assert usage.tokens_out > 0
    assert usage.latency_ms > 0
