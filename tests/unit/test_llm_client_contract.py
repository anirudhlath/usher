"""`LLMClientContract` against both implementations that need no container."""

import json
from typing import Any

import httpx

from tests.contract.llm_client_contract import LLMClientContract
from tests.fakes.llm_client import FakeLLMClient
from usher.adapters.llm.openai_compatible import OpenAICompatibleClient
from usher.ports.llm import LLMClient, LLMPurpose


class TestFakeLLMClient(LLMClientContract):
    def client(self) -> LLMClient:
        return FakeLLMClient.returning({"ok": True})


class TestOpenAICompatibleClient(LLMClientContract):
    def client(self) -> LLMClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            body: dict[str, Any] = {
                "model": "served/model-1",
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"ok": True})}}
                ],
            }
            return httpx.Response(200, json=body)

        return OpenAICompatibleClient(
            transport=httpx.MockTransport(handler),
            model="served/model-1",
            base_url="https://llm.invalid/v1",
        )


async def test_the_fake_records_the_temperature_a_caller_passed() -> None:
    client = FakeLLMClient.returning({"ok": True})
    await client.complete_json(
        "prompt", {}, purpose=LLMPurpose.BECAUSE_YOU_WATCHED, temperature=0.3
    )
    await client.complete_json("prompt", {}, purpose=LLMPurpose.CURATION)
    assert len(client.calls) == 2
    assert client.calls[0].temperature == 0.3
    assert client.calls[1].temperature is None
