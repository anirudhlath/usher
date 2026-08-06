"""`LLMClientContract` against both implementations that need no container.

The fake and the adapter are held to the same suite for the reason every
other contract suite in this repository exists: the two are only verified to
agree if something runs the same assertions against both. `tests/fakes/
llm_client.py`'s docstring enumerates the six places it is deliberately more
forgiving, and those are exactly the places this suite says nothing about.

A third subclass driving a **live** endpoint is in
`tests/integration/test_llm_client_live.py` and skips itself unless one is
configured.
"""

import json
from typing import Any

import httpx

from tests.contract.llm_client_contract import LLMClientContract
from tests.fakes.llm_client import FakeLLMClient
from usher.adapters.llm.openai_compatible import OpenAICompatibleClient
from usher.ports.llm import LLMClient


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
