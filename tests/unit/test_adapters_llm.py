"""`OpenAICompatibleClient` over `httpx.MockTransport`. No network.

Six things a scripted `FakeLLMClient` can never show, and each is a
measurement from this milestone's live probes rather than a defensive guess:

- **The fence.** With no `response_format` at all, 5 of 5 responses from a
  real endpoint were wrapped in a ` ```json ` fence, so `json.loads(content)`
  fails every time.
- **The status split.** M4 established against TMDb that a 4xx which is not a
  429 is `PortDataMalformed` rather than `PortUnavailable`, because five
  retries reach the identical answer. A single `raise PortUnavailable` arm
  passes every happy-path case.
- **The truncation.** `finish_reason == "length"` is the one failure that
  produces *valid* output: guided decoding closes the braces, the JSON parses,
  and rows are silently missing from the end of the list.
- **The credential.** It goes in an `Authorization` header and never in a URL,
  because `HTTPXClientInstrumentor` records the full URL as a span attribute.
- **The cost.** No provider reports it, so it is computed here from two
  configured prices ([ADR-0027]).
- **The latency.** A scripted fake *reports* a number; only this client
  *measures* one, and `CurationService._ledger_row` prefers whatever came back
  in the `LLMUsage` whenever one did -- so on every successful generation the
  number PRD 10's latency panel plots is the one computed here.
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

#: A JSON nesting depth past the one `json.loads` refuses. Measured on CPython
#: 3.13 at the default recursion limit of 1,000: **9,998 parses and 9,999
#: raises** `RecursionError` -- the C scanner has its own budget and it is an
#: order of magnitude past `sys.getrecursionlimit()`, which is why the obvious
#: guess of "a bit over 1,000" does not reach it and a case built on that guess
#: would pass against the unfixed code. Clear of the boundary rather than on
#: it: the exact number is an interpreter property, not this project's.
_DEEP = 12_000

#: Where the injected clock starts. **Deliberately not zero**, for the reason
#: `tests/unit/test_services_curation.py` gives one layer up: `time.monotonic`'s
#: epoch is arbitrary, so a fixture starting at `0.0` makes `clock() - started`
#: and `clock()` the identical number and an absolute reading is invisible on
#: the one field this client takes an injected clock in order to measure.
_T0 = 1_000.0

#: How long this file's transport takes to answer, and **why it is not the
#: 1,420 ms the live run measured as its median.** `_T0 + 1.42` is `1001.42`,
#: which is not representable in binary, so `int((1001.42 - 1000.0) * 1000)` is
#: **1419** -- an exact assertion on a measured-looking constant would have
#: been an off-by-one nobody could read as anything but a defect. 1.5 is
#: dyadic, so every step below is exact.
_SEND_SECONDS = 1.5

#: **A literal, deliberately not `int(_SEND_SECONDS * 1000)`**, and that is the
#: same finding rather than a second one: the derived spelling performs a
#: *different* computation from the client's, which subtracts first. Measured
#: -- at 1.42 they answer **1420** and **1419** -- so a derivation would agree
#: here, silently disagree the day somebody puts the measured median back, and
#: fail on the arithmetic rather than on the code. Change one, recompute the
#: other the way `complete_json` does.
_SEND_MS = 1_500


class _Clock:
    """A monotonic clock that moves only when the transport does.

    **A two-tick iterator cannot see this defect and that is the whole design
    of this fixture.** `iter([_T0, _T0 + elapsed])` hands out the same two
    numbers whether `started` is read before the send or after it, so both
    spellings compute the identical delta -- the fixture would be measuring the
    iterator rather than the code. Moving the clock *inside* the handler is
    what puts the request on one side of the reading and makes "the send is in
    the measured window" a thing an assertion can be wrong about.
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

    `response_format: json_schema` with `strict: true` was measured working
    against a live endpoint and is the cheapest guarantee available; the
    fallbacks below exist for providers that ignore it, not instead of it.
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
    """A local vLLM or Ollama needs none, and sending `Bearer None` is how a
    client fails against the deployment this project is actually for."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    await _complete(_client(handler, api_key=None))
    assert "authorization" not in seen[0].headers


async def test_the_purpose_is_telemetry_and_does_not_reach_the_provider() -> None:
    """`LLMPurpose` is `llm_calls.purpose`, a column in this project's own
    ledger. A client that put it in the request body would be inventing a
    field for somebody else's API."""
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
    """Kills `json.loads(content)`.

    Measured: against a live endpoint with no `response_format`, **5 of 5**
    responses came back inside a ` ```json ` fence. This is a fallback for a
    provider that ignores `response_format`, so it has to work on the shape
    that provider actually produces.
    """
    client = _client(body=_completion(wrapped))
    body, _usage = await _complete(client)
    assert body == {"ok": True}


async def test_content_that_is_not_json_is_malformed_not_unavailable() -> None:
    """A model that answered in prose is a permanent property of that prompt.
    Retrying five times reaches the same sentence."""
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion("I'm afraid I can't do that.")))


async def test_a_json_array_is_refused_because_the_port_promises_an_object() -> None:
    """`complete_json` is annotated `-> tuple[dict[str, Any], LLMUsage]`, and
    a list that reached a caller would fail on `body["rows"]` several frames
    away from the thing that was wrong."""
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion('[{"ok": true}]')))


async def test_a_response_with_no_choices_is_malformed() -> None:
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body={"model": "m", "choices": []}))


async def test_a_null_content_is_malformed() -> None:
    """Some providers return `content: null` alongside a tool call. Nothing
    here asks for tools, so a null is an answer this client cannot use --
    and `json.loads(None)` raises `TypeError`, which is not a
    `UsherPortError` and would take the worker down instead of parking one
    job."""
    with pytest.raises(PortDataMalformed):
        await _complete(_client(body=_completion(None)))  # type: ignore[arg-type]


async def test_deeply_nested_content_is_malformed_not_a_recursion_error() -> None:
    """Same family as the `content: null` case above, and missed for the same
    reason it was caught: `json.loads` raises `RecursionError` past a nesting
    depth of 9,999, and `RecursionError` subclasses `RuntimeError`, **not**
    `ValueError` -- so `_parse`'s `except ValueError` does not see it, it is
    not a `UsherPortError`, and it escapes `CurationService`'s
    `except UsherPortError` to take the worker down instead of parking one job.

    The depth is measured, not guessed: 9,998 parses and 9,999 raises on
    CPython 3.13 at the default recursion limit. `_DEEP` clears it with room
    to spare rather than sitting on the boundary, because the boundary is an
    interpreter property this case has no business pinning.

    Reachable on the two fallback paths the module docstring names -- the
    `json_object` arm and the fenced-prose arm -- which are unconstrained
    generation, and this project has already measured an unsatisfiable bound
    driving *this endpoint* into a degenerate repeating loop.
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
    """`latency_ms` is deliberately **not** asserted here: it is the one field
    of `LLMUsage` that is measured rather than read, so it belongs with the
    clock below. The `>= 0` bound this case used to carry could not fail --
    `max(0, ...)` clamps it -- and was the only assertion about latency
    anywhere in the repository.
    """
    _body, usage = await _complete(_client())
    assert usage.tokens_in == 1200
    assert usage.tokens_out == 340
    assert usage.model == "served/model-1"


async def test_cost_is_computed_from_the_configured_prices_in_decimal() -> None:
    """Kills float arithmetic and kills reading a cost field that does not
    exist.

    Measured against a live endpoint: `usage` carries `prompt_tokens`,
    `completion_tokens` and `total_tokens` and **no cost field at all**, so
    PRD 10's "litellm reports per-call cost natively" was wrong about the
    mechanism. 1200 in at $3/Mtok and 340 out at $15/Mtok is
    0.0036 + 0.0051 = 0.0087 exactly, which is a number binary floating point
    cannot represent.
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
    """A provider that omits `usage` has still answered, and failing the whole
    generation over a bookkeeping gap would trade good rows for an accurate
    ledger. The zeros are visible as zeros -- a real completion with a real
    latency and no tokens is obviously wrong on the dashboard -- and this is
    recorded in the module docstring rather than hidden."""
    _body, usage = await _complete(_client(body=_completion(json.dumps({"ok": True}), usage=None)))
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0
    assert usage.cost_usd == Decimal(0)


async def test_the_reported_model_falls_back_to_the_configured_one() -> None:
    """`usage.model` is what PRD 10 groups spend by, so an empty string
    collapses every model into one bar. A provider that echoes no `model` is
    still serving the one that was asked for."""
    body = _completion(json.dumps({"ok": True}))
    del body["model"]
    _b, usage = await _complete(_client(body=body))
    assert usage.model == "served/model-1"


# --------------------------------------------------------------------------
# The latency, which is the number that reaches the ledger


async def test_the_latency_is_the_whole_send_and_not_what_was_left_after_it() -> None:
    """**The success path's latency, pinned to the millisecond.**

    `CurationService._ledger_row` writes `latency_ms=usage.latency_ms` whenever
    a usage came back, so on every successful generation the number PRD 10's
    latency panel plots is this one -- the service's own stopwatch is the
    *fallback*, reached only when the call failed and there is no `LLMUsage` to
    read. Until this case the only assertion anywhere was `latency_ms >= 0`,
    which `max(0, ...)` makes unfalsifiable, and **no test in the repository
    ever passed this client a clock** although it takes one for exactly this.

    Two spellings of the defect, and this case exists for the second:

    - **The careless one** -- `int(self._clock() * 1000)`, an absolute reading
      of a clock whose epoch is arbitrary -- is caught by `ruff` as
      `F841 Local variable 'started' is assigned to but never used`. That is
      the gate holding it, not the suite. Killed here anyway: the assertion
      would read `1_001_500`.
    - **The careful one** -- `started` re-read *after* `await self._send(...)`,
      so the measured window excludes the request -- passes every gate step.
      It reports **0 ms** for a 1,500 ms completion, and a flat panel is the
      failure shape M8's live run already recorded a taste of: a 1,420 ms
      median that nothing in the suite could tell from zero.

    Same finding as M8 Task 12's `_T0`, one layer down and on the arm that
    matters more. That task fixed `CurationService`, whose measured number is
    only ever written on the **failure** path; the adapter's is written on the
    **success** path -- every ordinary night -- and was left with the same
    shape.
    """
    clock = _Clock()

    def handler(_request: httpx.Request) -> httpx.Response:
        # The request costs time, and it is the only thing here that does.
        clock.advance(_SEND_SECONDS)
        return httpx.Response(200, json=_completion(json.dumps({"ok": True})))

    _body, usage = await _complete(_client(handler, clock=clock))

    assert usage.latency_ms == _SEND_MS, "a delta across the send, not a reading beside it"


def test_the_clock_default_is_the_monotonic_one() -> None:
    """Pinned on the signature, because the behavioural version cannot fail.

    `time.monotonic` drifting to `time.time` is a genuine equivalent mutant
    here -- both reads come from the same callable, so the delta is identical
    -- and the two differ only across a wall-clock adjustment, which cannot be
    induced against a builtin used as a default. Measured and recorded the same
    way for `CurationService` and `QueryExpansionService`, which each pin their
    own default on the signature for the same reason.

    The half that *is* behavioural is the case above, and the two are not
    interchangeable: this one says which clock ships, that one says the
    reading is a delta across the send.
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

    M4 measured this against TMDb: a 400 for a request the provider will
    never accept costs five rate-limited retries and a whole backoff schedule
    to reach the identical answer, and then parks with "upstream unavailable"
    rather than with what was wrong. Here the three that matter are a bad
    schema (400), an unknown model (404) and a prompt over the context length
    (400 or 422 depending on provider) -- and the last is permanent for *that
    prompt*, whose fix is a smaller pool.
    """
    with pytest.raises(PortDataMalformed):
        await _complete(_client(status=status, body={}))


async def test_a_408_stays_retryable() -> None:
    """The one 4xx that really does mean "send this again". A household may
    put a proxy in front of a hosted provider, and a proxy that gives up
    waiting is exactly what the queue's backoff is for."""
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
    """PRD 08: credentials are never logged, including in error paths -- and
    an httpx transport exception's own text frequently includes the request
    URL. `EmbySession` interpolates one and explains why that is safe there;
    it is not safe here, because a household may be pointed at a provider
    whose URL carries a token in a path segment.
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
    """PRD 08: a rejected request never echoes the body it rejected -- and
    here that body is the household's watch history."""
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
