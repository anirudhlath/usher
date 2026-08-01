"""`TmdbClient` over `httpx.MockTransport`. No network, no real clock.

The three things a fixture-serving fake provider can never show: the
throttle, the status-code translation, and that the API key never reaches a
message. `tests/fakes/metadata_provider.py`'s own docstring names all three
as its divergences and points here.
"""

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from usher.adapters.tmdb.client import TmdbClient
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

_KEY = SecretStr("0123456789abcdef0123456789abcdef")
# A v4 "API Read Access Token" is a JWT. TMDb's own documentation says the
# bearer token works on v3 endpoints and that "both authentication methods
# provide the same level of access".
_V4_TOKEN = SecretStr("eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJzeW50aGV0aWMifQ.c3ludGhldGlj")


class _Clock:
    """A monotonic clock that only ever moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        # A real yield, so a gathered batch interleaves the way it would
        # against `asyncio.sleep` -- without it the "concurrent" case would
        # be a sequential one wearing a costume.
        await asyncio.sleep(0)


def _transport(
    handler: Any = None, *, status: int = 200, body: Any = None, headers: Any = None
) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": 1}, headers=headers)

    transport = httpx.MockTransport(handler or default)
    transport.seen = seen  # type: ignore[attr-defined]  # test affordance
    return transport


def _client(transport: httpx.MockTransport, **kwargs: Any) -> tuple[TmdbClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=transport, base_url="https://api.themoviedb.org/3")
    return TmdbClient(http, kwargs.pop("api_key", _KEY), **kwargs), http


# -- authentication --------------------------------------------------------


async def test_a_v3_key_is_sent_as_the_api_key_query_parameter() -> None:
    transport = _transport()
    client, http = _client(transport)
    async with http:
        await client.get("/movie/90000550")
    request = transport.seen[0]  # type: ignore[attr-defined]
    assert request.url.params["api_key"] == _KEY.get_secret_value()
    assert "authorization" not in request.headers


async def test_a_v4_read_access_token_is_sent_as_a_bearer_header_instead() -> None:
    """Not cosmetic. A query-parameter credential lands in every URL, and
    `HTTPXClientInstrumentor` records the full URL as a span attribute, so
    the v3 form writes the key into telemetry on every request. TMDb accepts
    the bearer token on v3 endpoints, so an operator who configures one gets
    that leak closed with no code change."""
    transport = _transport()
    client, http = _client(transport, api_key=_V4_TOKEN)
    async with http:
        await client.get("/movie/90000550")
    request = transport.seen[0]  # type: ignore[attr-defined]
    assert request.headers["authorization"] == f"Bearer {_V4_TOKEN.get_secret_value()}"
    assert "api_key" not in request.url.params


async def test_the_key_never_reaches_a_transport_failure_message() -> None:
    """PRD 08's "credentials are never logged", enforced rather than
    asserted. `EmbySession` interpolates the httpx exception into its own
    message safely because an Emby URL carries no credential; a TMDb v3 URL
    does, so this client may not."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, http = _client(_transport(refuse))
    async with http:
        with pytest.raises(PortUnavailable) as caught:
            await client.get("/movie/90000550")
    assert _KEY.get_secret_value() not in str(caught.value)
    assert "/movie/90000550" in str(caught.value)


# -- status translation ----------------------------------------------------


async def test_a_404_is_malformed_data_not_an_outage() -> None:
    """The branch that makes `JobWorker`'s park-immediately path fire in
    production. A TMDb id the catalog holds that TMDb no longer serves is a
    wrong answer, not an outage -- and the catalog holds 291,737 TMDb ids
    from a bulk export that ages. Translating it to `PortUnavailable` spends
    five rate-limited retries before parking with the wrong reason."""
    client, http = _client(
        _transport(status=404, body={"success": False, "status_code": 34, "status_message": "x"})
    )
    async with http:
        with pytest.raises(PortDataMalformed):
            await client.get("/movie/999999999")


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_is_an_auth_failure(status: int) -> None:
    client, http = _client(_transport(status=status))
    async with http:
        with pytest.raises(PortAuthFailed):
            await client.get("/movie/90000550")


async def test_a_429_carries_the_retry_after_hint() -> None:
    client, http = _client(_transport(status=429, headers={"retry-after": "17"}))
    async with http:
        with pytest.raises(PortRateLimited) as caught:
            await client.get("/movie/90000550")
    assert caught.value.retry_after == 17.0


async def test_a_429_with_an_http_date_retry_after_is_still_a_hint() -> None:
    """RFC 9110 permits either form and `float(value)` raises on the second.
    `usher.adapters.http.retry_after_seconds` is shared for exactly this;
    the bug it fixes existed in two places."""
    client, http = _client(
        _transport(status=429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    )
    async with http:
        with pytest.raises(PortRateLimited) as caught:
            await client.get("/movie/90000550")
    assert caught.value.retry_after is not None


async def test_a_429_without_a_hint_is_still_a_rate_limit() -> None:
    client, http = _client(_transport(status=429))
    async with http:
        with pytest.raises(PortRateLimited) as caught:
            await client.get("/movie/90000550")
    assert caught.value.retry_after is None


async def test_a_server_error_is_an_outage() -> None:
    client, http = _client(_transport(status=503))
    async with http:
        with pytest.raises(PortUnavailable):
            await client.get("/movie/90000550")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # Live 2026-08-01: `/movie/changes` with a 15-day window.
        (
            422,
            {
                "success": False,
                "status_code": 20,
                "status_message": "Invalid date range: Should be a range no longer than 14 days.",
            },
        ),
        # Live 2026-08-01: 21 `append_to_response` items.
        (
            400,
            {
                "success": False,
                "status_code": 27,
                "status_message": (
                    "Too many append to response objects: The maximum number of remote calls is 20."
                ),
            },
        ),
    ],
)
async def test_a_rejected_request_is_malformed_data_not_an_outage(status: int, body: Any) -> None:
    """A 4xx that is not a 429 cannot become an answer by being sent again.

    Both bodies are the ones TMDb really returned on 2026-08-01, and both
    are permanent properties of the *request*: a 15-day change window and a
    21-item `append_to_response`. Translated as `PortUnavailable` they are
    retryable, so `JobWorker` spends five rate-limited attempts and a
    backoff schedule on a request that can never succeed, then parks with
    "upstream unavailable" rather than with what was actually wrong. Same
    argument the 404 case above makes, arriving from the other four
    statuses TMDb has been observed to use.
    """
    client, http = _client(_transport(status=status, body=body))
    async with http:
        with pytest.raises(PortDataMalformed):
            await client.get("/movie/90000550")


async def test_a_request_timeout_is_still_an_outage() -> None:
    """The one 4xx that is *not* the caller's fault. TMDb itself has never
    been observed to send it, but `Settings.tmdb_base_url` exists precisely
    so a household can put a proxy in front of TMDb, and a proxy that gives
    up waiting is exactly the transient failure the queue's backoff is
    for."""
    client, http = _client(_transport(status=408))
    async with http:
        with pytest.raises(PortUnavailable):
            await client.get("/movie/90000550")


async def test_a_non_json_body_is_malformed() -> None:
    """A reverse proxy or a captive portal serving HTML with status 200. A
    raw `json.JSONDecodeError` escaping the port is not something a caller
    written against `usher.ports.errors` can catch."""

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    client, http = _client(_transport(html))
    async with http:
        with pytest.raises(PortDataMalformed):
            await client.get("/movie/90000550")


async def test_a_json_array_body_is_malformed() -> None:
    def array(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client, http = _client(_transport(array))
    async with http:
        with pytest.raises(PortDataMalformed):
            await client.get("/movie/90000550")


# -- the throttle ----------------------------------------------------------


async def test_the_throttle_holds_requests_to_the_configured_rate() -> None:
    """PRD 10's dashboard 3 plots "TMDb requests/sec against the ~40
    ceiling"; TMDb's own documentation says the limits "sit somewhere in the
    40 requests per second range" and to "respect the 429 if you receive
    one". Asserted against an injected clock rather than by sleeping.

    Six requests at two per second: the first two spend the bucket's burst
    and the remaining four wait half a second each.
    """
    clock = _Clock()
    client, http = _client(_transport(), requests_per_second=2.0, clock=clock, sleep=clock.sleep)
    async with http:
        for _ in range(6):
            await client.get("/movie/90000550")
    assert clock.now == pytest.approx(2.0)


async def test_the_throttle_survives_concurrency() -> None:
    """A per-call check with no lock lets N coroutines all read the same
    token count and all decide they may go -- which is a burst of N against
    a limit of one, and the failure only appears under concurrency."""
    clock = _Clock()
    client, http = _client(_transport(), requests_per_second=2.0, clock=clock, sleep=clock.sleep)
    async with http:
        await asyncio.gather(*(client.get("/movie/90000550") for _ in range(6)))
    assert clock.now == pytest.approx(2.0)


async def test_a_burst_within_the_budget_does_not_wait() -> None:
    """A throttle that slept before every request would halve the throughput
    of a walk that is already under the ceiling."""
    clock = _Clock()
    client, http = _client(_transport(), requests_per_second=10.0, clock=clock, sleep=clock.sleep)
    async with http:
        for _ in range(10):
            await client.get("/movie/90000550")
    assert clock.now == 0.0
