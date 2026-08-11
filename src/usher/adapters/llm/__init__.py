"""The `LLMClient` implementation.

Capability-named rather than upstream-named, and the reason changed in M8.
[PRD 01](../../../../docs/prd/01-architecture.md)'s naming rule gives a
directory the upstream's name "when a port's implementation talks to one
nameable external service", and the reason `llm/` was exempt used to be that
`litellm` is itself a multi-provider abstraction. `litellm` is not taken
([ADR-0027](../../../../docs/prd/decisions/0027-the-llm-client-is-one-http-call.md)),
and the exemption holds for a different reason: an OpenAI-compatible client's
upstream is whatever `USHER_LLM_BASE_URL` points at, which is a setting rather
than a name. The same deployment runs it against a vLLM on localhost and
against OpenRouter, unchanged.
"""

from usher.adapters.llm.openai_compatible import OpenAICompatibleClient

__all__ = ["OpenAICompatibleClient"]
