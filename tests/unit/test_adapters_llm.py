"""`OpenAICompatibleClient` over `httpx.MockTransport`.

No network.
"""

import inspect
import json
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from usher.adapters.llm.openai_compatible import OpenAICompatibleClient
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)
from usher.ports.llm import LLMPurpose

_KEY = SecretStr("sk-0000000000000000000000000000000000000000000000")
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


# A sentinel, because `None` is a meaningful value here -- a provider that
# omits `usage` entirely is a real shape and one case is about it.
_REPORTED = object()

#: A JSON nesting depth past the one `json.loads` refuses.
_DEEP = 12_000

#: Where the injected clock starts. **Deliberately not zero**, for the reason
#: `tests/unit/test_services_curation.py` gives one layer up: `time.monotonic`'s
#: epoch is arbitrary, so a fixture starting at `0.0` makes `clock() - started`
#: and `clock()` the identical number and an absolute reading is invisible on
#: the one field this client takes an injected clock in order to measure.
_T0 = 1_000.0

#: How long this file's transport takes to answer. A round number on purpose: an
#: interval whose seconds form is not representable in binary turns an exact
#: millisecond assertion into an off-by-one nobody reads as anything but a defect.
_SEND_SECONDS = 1.5

#: A literal, deliberately not `int(_SEND_SECONDS * 1000)`: the derived spelling
#: performs a different computation from the client's, which subtracts first.
_SEND_MS = 1_500


class _Clock:
    """A monotonic clock that moves only when the transport does.

    `iter([_T0, _T0 + elapsed])` hands out the same two numbers whether `started`
    is read before the send or after it, so both spellings compute the identical
    delta and the fixture would be testing the iterator rather than the code.
    Moving the clock *inside* the handler puts the request on one side of the
    reading, which is what makes "the send is inside the window" a thing an
    assertion can be wrong about.
    """

    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _completion(
    content: str,
    *,
    usage: object = _REPORTED,
    finish_reason: str = "stop",
    model: str = "served/model-1",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"content": content}}],
    }
    if usage is _REPORTED:
        body["usage"] = {"prompt_tokens": 1200, "completion_tokens": 340}
    elif usage is not None:
        body["usage"] = usage
    return body


def _client(
    handler: Any = None,
    *,
    status: int = 200,
    body: Any = None,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> OpenAICompatibleClient:
    if handler is None:

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                json=body if body is not None else _completion(json.dumps({"ok": True})),
                headers=headers,
            )

    kwargs.setdefault("model", "served/model-1")
    kwargs.setdefault("base_url", "https://llm.invalid/v1")
    kwargs.setdefault("api_key", _KEY)
    return OpenAICompatibleClient(transport=httpx.MockTransport(handler), **kwargs)


async def _complete(client: OpenAICompatibleClient) -> tuple[dict[str, Any], Any]:
    try:
        return await client.complete_json("prompt", _SCHEMA, purpose=LLMPurpose.CURATION)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# The request


async def test_the_request_asks_for_the_schema_by_name_and_strictly() -> None:
    """Kills a client that sends the schema in the prompt and hopes.

    `response_format: json_schema` with `strict: true` is the cheapest guarantee
    available; the fallbacks below exist for providers that ignore it, not instead
    of it.
    """
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    await _complete(_client(handler))
    fmt = seen[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == _SCHEMA


async def test_the_credential_is_a_header_and_never_reaches_the_url() -> None:
    """Kills `?api_key=`.

    `HTTPXClientInstrumentor` is wired in `configure_tracing` and records the
    full URL as a span attribute, so a query-parameter credential is written
    into telemetry on every request. TMDb v3 forces that spelling and this
    protocol does not.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    await _complete(_client(handler))
    assert seen[0].headers["authorization"] == f"Bearer {_KEY.get_secret_value()}"
    assert _KEY.get_secret_value() not in str(seen[0].url)


async def test_no_credential_configured_sends_no_authorization_header() -> None:
    """A local vLLM or Ollama needs no key, so no header is sent without one.

    Sending `Bearer None` is how a client fails against the deployment this
    project is actually for.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    await _complete(_client(handler, api_key=None))
    assert "authorization" not in seen[0].headers


async def test_the_purpose_is_telemetry_and_does_not_reach_the_provider() -> None:
    """`LLMPurpose` is `llm_calls.purpose`, a column in this project's own ledger.

    A client that put it in the request body would be inventing a field for somebody
    else's API.
    """
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    await _complete(_client(handler))
    assert "purpose" not in seen[0]
    assert "curation" not in json.dumps(seen[0])


# --------------------------------------------------------------------------
# Parsing the response


async def test_a_plain_json_body_parses() -> None:
    body, _usage = await _complete(_client())
    assert body == {"ok": True}


@pytest.mark.parametrize(
    "wrapped",
    [
        '```json\n{"ok": true}\n```',
        '```\n{"ok": true}\n```',
        '```json{"ok": true}```',
        '  ```json\n{"ok": true}\n```  ',
    ],
)
async def test_a_fenced_body_parses(wrapped: str) -> None:
    """Kills `json.loads(content)` on a fenced answer.

    A provider that ignores `response_format` routinely wraps its answer in a
    ` ```json ` fence, and this is the fallback for that, so it has to work on the
    shape such a provider produces.
    """
    client = _client(body=_completion(wrapped))
    body, _usage = await _complete(client)
    assert body == {"ok": True}


async def test_content_that_is_not_json_is_malformed_not_unavailable() -> None:
    """A model that answered in prose is a permanent property of that prompt.

    Retrying five times reaches the same sentence.
    """
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion("I'm afraid I can't do that.")))


async def test_a_json_array_is_refused_because_the_port_promises_an_object() -> None:
    """A top-level JSON array is refused rather than handed back.

    `complete_json` promises a mapping, and a list that reached a caller would
    fail on `body["rows"]` several frames away from the thing that was wrong.
    """
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion('[{"ok": true}]')))


async def test_a_response_with_no_choices_is_malformed() -> None:
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body={"model": "m", "choices": []}))


async def test_a_null_content_is_malformed() -> None:
    """Some providers return `content: null` alongside a tool call.

    Nothing here asks for tools, so a null is an answer this client cannot use -- and
    `json.loads(None)` raises `TypeError`, which is not a `UsherPortError` and would
    take the worker down instead of parking one job.
    """
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion(None)))  # type: ignore[arg-type]


async def test_deeply_nested_content_is_malformed_not_a_recursion_error() -> None:
    """Nesting past `json.loads`'s limit raises outside the port taxonomy.

    `json.loads` raises `RecursionError` on deep enough nesting, and
    `RecursionError` subclasses `RuntimeError` rather than `ValueError` -- so
    `_parse`'s `except ValueError` does not see it, it is not a `UsherPortError`,
    and it escapes `CurationService`'s `except UsherPortError` to take the worker
    down instead of parking one job. `_DEEP` clears the limit with room to spare
    rather than sitting on the boundary, which is an interpreter property this
    case has no business pinning. Reachable on the two fallback paths the module
    docstring names, both of which are unconstrained generation.
    """
    nested = "[" * _DEEP + "]" * _DEEP
    # The premise: this really is the exception the port does not classify,
    # and it really does escape a bare `except ValueError`. Asserted rather
    # than assumed, because a case whose subject is an interpreter limit is
    # one a later CPython could quietly stop exercising.
    with pytest.raises(RecursionError):
        try:
            json.loads(nested)
        except ValueError:  # pragma: no cover - the point is that it does not fire
            pytest.fail("json.loads raised a ValueError; this case pins the other branch")
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion(nested)))


async def test_a_deeply_nested_envelope_is_malformed_not_a_recursion_error() -> None:
    """The same defect one layer out, and the layer that is actually exposed.

    `_parse`'s half is largely shielded by `_content`, which refuses
    `finish_reason == "length"` before anything is parsed -- and a model that
    ran away into 10,000 open brackets hits the token ceiling first. The
    *envelope* has no such guard and no token bound at all: it is whatever the
    endpoint, or a proxy in front of it, put on the wire. `response.json()`
    raises the same unclassified `RecursionError` from `_decode`.
    """
    nested = ('{"a":' * _DEEP) + "null" + ("}" * _DEEP)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=nested.encode(), headers={"content-type": "application/json"}
        )

    with pytest.raises(PortDataMalformed):
        await _complete(_client(handler))


async def test_a_truncated_completion_is_refused_and_names_the_cap() -> None:
    """Kills trusting `json.loads` to notice.

    This is the one failure that produces *valid* output: under guided
    decoding the provider closes the braces at the token limit, so the JSON
    parses and rows are simply missing from the end of the list. Nothing
    downstream can tell a truncated generation from a short one -- the
    validator sees three well-formed rows and writes them.
    """
    client = _client(body=_completion(json.dumps({"ok": True}), finish_reason="length"))
    with pytest.raises(PortDataMalformed) as raised:
        await _complete(client)
    assert "max_output_tokens" in str(raised.value) or "truncat" in str(raised.value).lower()


# --------------------------------------------------------------------------
# Usage and cost


async def test_usage_is_read_from_the_response() -> None:
    """`latency_ms` is deliberately not asserted here.

    It is the one field of `LLMUsage` the client computes rather than reads, so it
    belongs with the clock below. A `>= 0` bound here could not fail anyway,
    because `max(0, ...)` clamps it.
    """
    _body, usage = await _complete(_client())
    assert usage.tokens_in == 1200
    assert usage.tokens_out == 340
    assert usage.model == "served/model-1"


async def test_cost_is_computed_from_the_configured_prices_in_decimal() -> None:
    """Kills float arithmetic and kills reading a cost field that does not exist.

    `usage` carries `prompt_tokens`, `completion_tokens` and `total_tokens` and no
    cost field at all, so the client computes the cost itself. 1200 in at $3/Mtok
    and 340 out at $15/Mtok is 0.0036 + 0.0051 = 0.0087 exactly, which is a number
    binary floating point cannot represent.
    """
    client = _client(
        price_in_per_mtok=Decimal("3.00"),
        price_out_per_mtok=Decimal("15.00"),
    )
    _body, usage = await _complete(client)
    assert isinstance(usage.cost_usd, Decimal)
    assert usage.cost_usd == Decimal("0.0087")


async def test_the_default_prices_are_zero_which_is_honest_for_a_local_model() -> None:
    _body, usage = await _complete(_client())
    assert usage.cost_usd == Decimal(0)


async def test_a_response_with_no_usage_reports_zeros_rather_than_failing() -> None:
    """A provider that omits `usage` has still answered, so zeros are recorded.

    Failing the whole generation over a bookkeeping gap would trade good rows for
    an accurate ledger. The zeros stay visible as zeros: a real completion with a
    real latency and no tokens is obviously wrong on a dashboard.
    """
    _body, usage = await _complete(_client(body=_completion(json.dumps({"ok": True}), usage=None)))
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0
    assert usage.cost_usd == Decimal(0)


async def test_the_reported_model_falls_back_to_the_configured_one() -> None:
    """`usage.model` falls back to the model that was asked for.

    Spend is grouped by it, so an empty string collapses every model into one bar,
    and a provider that echoes no `model` is still serving the one requested.
    """
    body = _completion(json.dumps({"ok": True}))
    del body["model"]
    _b, usage = await _complete(_client(body=body))
    assert usage.model == "served/model-1"


# --------------------------------------------------------------------------
# The latency, which is the number that reaches the ledger


async def test_the_latency_is_the_whole_send_and_not_what_was_left_after_it() -> None:
    """The success path's latency, pinned to the millisecond."""
    clock = _Clock()

    def handler(_request: httpx.Request) -> httpx.Response:
        # The request costs time, and it is the only thing here that does.
        clock.advance(_SEND_SECONDS)
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    _body, usage = await _complete(_client(handler, clock=clock))

    assert usage.latency_ms == _SEND_MS, "a delta across the send, not a reading beside it"


def test_the_clock_default_is_the_monotonic_one() -> None:
    """Pinned on the signature, because the behavioural version cannot fail.

    `time.monotonic` drifting to `time.time` is a genuine equivalent mutant here --
    both reads come from the same callable, so the delta is identical -- and the
    two differ only across a wall-clock adjustment, which cannot be induced
    against a builtin used as a default. `CurationService` and
    `QueryExpansionService` each pin their own default on the signature for the
    same reason. The behavioural half is the case above: this one says which clock
    ships, that one says the reading is a delta across the send.
    """
    default = inspect.signature(OpenAICompatibleClient.__init__).parameters["clock"].default

    assert default is time.monotonic
    assert time.monotonic is not time.time, "the premise: these are two different clocks"


# --------------------------------------------------------------------------
# The status taxonomy


async def test_a_429_is_rate_limited_and_reads_retry_after() -> None:
    client = _client(status=429, body={}, headers={"retry-after": "7"})
    with pytest.raises(PortRateLimited) as raised:
        await _complete(client)
    assert raised.value.retry_after == pytest.approx(7.0)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_credential_is_auth_failed(status: int) -> None:
    with pytest.raises(PortAuthFailed):
        await _complete(_client(status=status, body={}))


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_a_permanent_4xx_is_malformed_not_unavailable(status: int) -> None:
    """Kills one `except HTTPStatusError` arm raising `PortUnavailable`.

    A 4xx for a request the provider will never accept costs five rate-limited
    retries and a whole backoff schedule to reach the identical answer, then parks
    with "upstream unavailable" rather than with what was wrong. The three that
    matter are a bad schema (400), an unknown model (404) and a prompt over the
    context length (400 or 422 by provider) -- the last permanent for *that*
    prompt, whose fix is a smaller pool.
    """
    with pytest.raises(PortDataMalformed):
        await _complete(_client(status=status, body={}))


async def test_a_408_stays_retryable() -> None:
    """The one 4xx that really does mean "send this again".

    A household may put a proxy in front of a hosted provider, and a proxy that gives up
    waiting is exactly what the queue's backoff is for.
    """
    with pytest.raises(PortUnavailable):
        await _complete(_client(status=408, body={}))


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_5xx_is_unavailable(status: int) -> None:
    with pytest.raises(PortUnavailable):
        await _complete(_client(status=status, body={}))


async def test_a_transport_failure_is_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with pytest.raises(PortUnavailable):
        await _complete(_client(handler))


async def test_no_failure_message_carries_the_credential_or_the_url() -> None:
    """Credentials are never logged, error paths included.

    An httpx transport exception's own text frequently includes the request URL,
    and a household may be pointed at a provider whose URL carries a token in a
    path segment.
    """
    secret = _KEY.get_secret_value()

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to https://llm.invalid/v1?key={secret}")

    for status in (400, 401, 429, 500):
        with pytest.raises(Exception) as raised:  # any port error will do
            await _complete(_client(status=status, body={}))
        assert secret not in str(raised.value)
        assert "llm.invalid" not in str(raised.value)

    with pytest.raises(PortUnavailable) as transport_failure:
        await _complete(_client(refuse))
    assert secret not in str(transport_failure.value)
    assert "llm.invalid" not in str(transport_failure.value)


async def test_a_rejected_request_does_not_echo_the_prompt() -> None:
    """A rejected request never echoes the body it rejected.

    Here that body is the household's watch history.
    """
    client = _client(status=400, body={"error": {"message": "bad request"}})
    with pytest.raises(PortDataMalformed) as raised:
        await client.complete_json(
            "the household watched Solaris", _SCHEMA, purpose=LLMPurpose.CURATION
        )
    await client.aclose()
    assert "Solaris" not in str(raised.value)


# --------------------------------------------------------------------------
# Lifecycle


async def test_aclose_releases_the_pool_and_is_idempotent() -> None:
    client = _client()
    await client.complete_json("prompt", _SCHEMA, purpose=LLMPurpose.CURATION)
    await client.aclose()
    await client.aclose()
    with pytest.raises(PortUnavailable):
        await client.complete_json("prompt", _SCHEMA, purpose=LLMPurpose.CURATION)
