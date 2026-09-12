"""One `POST /chat/completions` against any OpenAI-compatible endpoint."""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import SecretStr

from usher.adapters.http import UNTRANSLATED_FAILURES, decode_json, port_error_for
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.llm import LLMClient, LLMPurpose, LLMUsage

_COMPLETIONS_PATH = "/chat/completions"

# How this endpoint is named in a message. A constant and never `base_url`,
# never a URL and never a path built from one: a household may be pointed at a
# provider whose URL carries a token in a path segment, which is also why
# nothing below passes a `detail` to `usher.adapters.http`.
_ENDPOINT = "the LLM endpoint"

_TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)

_tracer = trace.get_tracer("usher.llm")


def _strip_fence(content: str) -> str:
    """Remove a Markdown code fence, if the answer came wrapped in one.

    Measured against a live endpoint with no `response_format`: **5 of 5**
    responses were fenced, so this is the shape the third fallback actually
    has to handle. Deliberately tolerant about the language tag and about
    whether a newline follows it, and deliberately *not* a regex over the
    whole string -- a fence-stripper that searched for the first `{` would
    also "succeed" on prose containing a brace, which is a parse of
    something nobody sent.
    """
    text = content.strip()
    if not text.startswith("```"):
        return text
    text = text[3:]
    newline = text.find("\n")
    first_line = text[:newline] if newline != -1 else text
    # ```json{...}``` -- no newline after the tag, observed.
    if first_line.strip().isalpha():
        text = text[newline + 1 :] if newline != -1 else ""
    elif text[:4].lower() == "json":
        text = text[4:]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    return text.strip()


class OpenAICompatibleClient(LLMClient):
    """`LLMClient` over `POST {base_url}/chat/completions`."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: SecretStr | None = None,
        max_output_tokens: int = 2048,
        timeout_seconds: float = 120.0,
        price_in_per_mtok: Decimal = Decimal(0),
        price_out_per_mtok: Decimal = Decimal(0),
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens
        self._price_in = price_in_per_mtok
        self._price_out = price_out_per_mtok
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(timeout_seconds)
        )

    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: LLMPurpose,
    ) -> tuple[dict[str, Any], LLMUsage]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_output_tokens,
            "temperature": 0.8,
            "response_format": {
                "type": "json_schema",
                # `strict` is what makes the schema a decoding constraint
                # rather than a suggestion, on the providers that honour it.
                "json_schema": {"name": "usher_response", "schema": schema, "strict": True},
            },
        }
        headers: dict[str, str] = {"content-type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"

        started = self._clock()
        # `purpose` is a column in *this* project's ledger, never a field in
        # somebody else's API, so it is a span attribute and nothing more.
        with _tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("usher.llm.purpose", purpose.value)
            span.set_attribute("usher.llm.model", self._model)
            response = await self._send(payload, headers)
            span.set_attribute("http.response.status_code", response.status_code)
            body = self._decode(response)
            latency_ms = max(0, int((self._clock() - started) * 1000))
            content = self._content(body)
            usage = self._as_usage(body, latency_ms)
            span.set_attribute("usher.llm.tokens_out", usage.tokens_out)
            return self._parse(content), usage

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------------- send

    async def _send(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        try:
            return await self._client.post(
                f"{self._base_url}{_COMPLETIONS_PATH}", json=payload, headers=headers
            )
        except UNTRANSLATED_FAILURES as exc:
            # `type(exc).__name__`, never `exc`: httpx's own text for several
            # transport failures includes the request URL, and a household
            # may be pointed at a provider whose URL carries a token.
            raise PortUnavailable(f"POST {_COMPLETIONS_PATH} failed: {type(exc).__name__}") from exc

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        """Status first, then JSON, both from `usher.adapters.http`.

        The ladder is `TmdbClient`'s ladder -- same four branches in the same
        order, and the M4-against-TMDb measurements that justify them are
        recorded with it rather than restated here. What this method still owns
        is what it hands over: **no branch may interpolate the response body,
        the URL or the prompt**, so neither call gets a `detail` and both are
        given the `_ENDPOINT` constant as their subject. The one bounded
        exception is the status code itself, which is a number.

        `decode_json`'s `RecursionError` arm is the exposed half of a pair --
        `_parse`'s subject is bounded by `max_output_tokens` and shielded by
        the truncation guard, while the envelope is whatever the endpoint, or a
        proxy in front of it, put on the wire.
        """
        error = port_error_for(response, what=_ENDPOINT, request_line=f"POST {_COMPLETIONS_PATH}")
        if error is not None:
            raise error
        return decode_json(response, what=_ENDPOINT)

    # ---------------------------------------------------------------- parse

    def _content(self, body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PortDataMalformed("the completion carried no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise PortDataMalformed("the completion's first choice was not an object")
        if choice.get("finish_reason") == "length":
            # Valid JSON with rows missing off the end. Caught here because
            # it is indistinguishable from a short answer everywhere else.
            raise PortDataMalformed(
                "the completion was truncated at the token ceiling; "
                "raise max_output_tokens or shrink the pool"
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            # `content: null` alongside a tool call is a real provider shape,
            # and `json.loads(None)` raises `TypeError` -- not a
            # `UsherPortError`, so it would take the worker process down
            # instead of parking one job.
            raise PortDataMalformed("the completion carried no text content")
        return content

    def _parse(self, content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(_strip_fence(content))
        except (ValueError, RecursionError) as exc:
            # See `_decode`. Reachable here on the two unconstrained-generation
            # fallbacks this module's docstring names, where a degenerate
            # repeating loop is a shape this project has already measured.
            raise PortDataMalformed("the completion was not JSON") from exc
        if not isinstance(parsed, dict):
            # The port is annotated `-> tuple[dict[str, Any], LLMUsage]`, and
            # a list reaching a caller fails on `body["rows"]` several frames
            # away from the thing that was actually wrong.
            raise PortDataMalformed(f"the completion was a {type(parsed).__name__}, not an object")
        return parsed

    # ---------------------------------------------------------------- usage

    def _usage(self, body: dict[str, Any]) -> tuple[int, int]:
        """`(tokens_in, tokens_out)`, zero when the provider reported none.

        Zeros rather than a refusal: a provider that omits `usage` has still
        answered, and failing the whole generation over a bookkeeping gap
        trades good rows for an accurate ledger. A row with a real latency and
        no tokens is visibly wrong on PRD 10's dashboard, which is the honest
        version of this compromise.
        """
        reported = body.get("usage")
        if not isinstance(reported, dict):
            return 0, 0
        tokens_in = reported.get("prompt_tokens")
        tokens_out = reported.get("completion_tokens")
        return (
            tokens_in if isinstance(tokens_in, int) else 0,
            tokens_out if isinstance(tokens_out, int) else 0,
        )

    def _as_usage(self, body: dict[str, Any], latency_ms: int) -> LLMUsage:
        tokens_in, tokens_out = self._usage(body)
        reported_model = body.get("model")
        return LLMUsage(
            # The configured model when the provider echoes none: `model` is
            # what PRD 10 groups spend by, and an empty string collapses every
            # model into one bar.
            model=(
                reported_model
                if isinstance(reported_model, str) and reported_model
                else self._model
            ),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._cost(tokens_in, tokens_out),
            latency_ms=latency_ms,
        )

    def _cost(self, tokens_in: int, tokens_out: int) -> Decimal:
        """Computed, never read -- no provider reports it.

        `Decimal` throughout and never via `float`: 1,200 in at $3/Mtok plus
        340 out at $15/Mtok is exactly 0.0087, which binary floating point
        cannot represent, and this number is summed over a month.
        """
        return (
            Decimal(tokens_in) * self._price_in + Decimal(tokens_out) * self._price_out
        ) / _TOKENS_PER_PRICE_UNIT


__all__ = ["OpenAICompatibleClient"]
