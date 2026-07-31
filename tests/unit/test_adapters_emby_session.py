"""EmbySession: the durable-client header, silent re-authentication, and
error translation. Driven entirely by httpx.MockTransport -- no network.
"""

import asyncio
import io
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_server import SERVER_VERSION, USER_ID, FakeEmbyServer
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby import session as session_module
from usher.adapters.emby.session import PUBLIC_INFO_PATH, SYSTEM_INFO_PATH, EmbySession
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)
from usher.ports.source import SourceItem, SourceItemKind

DEVICE_ID = "9d1f0b6c-0000-7000-8000-000000000001"
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
ITEM = SourceItem(
    external_id="movie-1", name="Example Movie", kind=SourceItemKind.MOVIE, container="mkv"
)


class _Clock:
    """An injected monotonic clock, so the re-auth cooldown's *expiry* is
    testable without a real sleep.

    Frozen: `now` only moves when a test moves it. The same clock also
    times `usher.source.request.duration`, deliberately -- one time source
    per session rather than two constructor knobs that can disagree -- so
    every duration recorded under *this* clock is exactly `0.0`. The one
    test that asserts on a duration uses `_TickingClock` below instead.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _TickingClock:
    """A monotonic clock that advances by `step` on every read, so the
    elapsed time `_send` measures is a known non-zero value."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class _RecordingHistogram:
    """Stands in for the module's `usher.source.request.duration` histogram.

    A real `MeterProvider` is not usable here: OpenTelemetry permits
    `set_meter_provider` exactly once per process and warns-and-ignores
    every later call, so a metrics assertion built on one is decided by
    whichever test in the session happened to run first.
    """

    def __init__(self) -> None:
        self.records: list[tuple[float, dict[str, Any]]] = []

    def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        self.records.append((amount, dict(attributes or {})))


def _session(
    server: FakeEmbyServer,
    *,
    source_name: str = "Living Room Emby",
    credentials: SourceCredentials = CREDENTIALS,
    clock: _Clock | _TickingClock | None = None,
) -> tuple[EmbySession, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=server.transport(), base_url="https://emby.invalid")
    session = EmbySession(
        client,
        credentials,
        source_name=source_name,
        device_id=DEVICE_ID,
        app_version="0.1.0",
        reauth_cooldown_seconds=60.0,
        clock=clock or _Clock(),
    )
    return session, client


async def test_the_durable_client_header_names_usher_and_the_device() -> None:
    """PRD 03's `Authorization: MediaBrowser Client="Usher", Device=…,
    DeviceId=…, Version=…`. The fake rejects an authentication without it,
    so this fails loudly rather than subtly if the header is dropped."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.device_ids == [DEVICE_ID]
    assert server.devices == ["Living Room Emby"]


async def test_the_identity_header_rides_on_every_request_not_just_authentication() -> None:
    """The defining property of the durable client, and the half the fake
    used to model in only one place. Emby attributes traffic to a device
    per *request*: an `Authorization` header sent only to
    `AuthenticateByName` mints one correctly-named session and then files
    every subsequent call under an anonymous client, which is the
    accumulating-pile-of-sessions failure arrived at from a third
    direction.

    The fake now rejects any request without it, on every route, so
    dropping `Authorization` from `_headers()` fails loudly here instead
    of passing all eighteen of this file's other cases.
    """
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.requests == ["POST /Users/AuthenticateByName", "GET /System/Info"]
    assert len(server.identities) == len(server.requests)
    for identity in server.identities:
        assert identity is not None
        assert identity.startswith("MediaBrowser ")
        assert 'Client="Usher"' in identity
        assert f'DeviceId="{DEVICE_ID}"' in identity
        assert 'Version="0.1.0"' in identity


async def test_the_same_device_id_is_reused_across_reauthentication() -> None:
    """The durable-client invariant, and the whole reason `device_id` is
    persisted on the `Source` row: a new id per authentication makes Usher
    an accumulating pile of sessions in Emby's dashboard, which is exactly
    what PRD 03 designed it not to be."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 2
    assert server.device_ids == [DEVICE_ID, DEVICE_ID]


async def test_a_source_name_with_quotes_cannot_break_the_header() -> None:
    """`My "Home" Emby` is a name an operator can type straight into
    `POST /admin/sources`. Interpolated raw it closes the quoted field
    early and Emby parses the header as something else entirely."""
    server = FakeEmbyServer()
    session, client = _session(server, source_name='My "Home" Emby')
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.devices == ["My _Home_ Emby"]


async def test_an_expired_session_is_silently_re_minted() -> None:
    """The failure that motivated this project: a token that silently
    started returning 401 with no way to renew it. No human pastes
    anything here."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        body = await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert body["Id"]
    assert server.authentications == 2


async def test_concurrent_401s_produce_one_authentication() -> None:
    """Single flight. Eight in-flight requests all hitting an expired
    session must not mint eight sessions -- the pile-of-sessions failure
    again, arrived at from the other direction."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await asyncio.gather(
            *(session.json_body("GET", SYSTEM_INFO_PATH, op="info") for _ in range(8))
        )
    finally:
        await client.aclose()
    assert server.authentications == 2


async def test_concurrent_401s_are_provably_simultaneous_and_produce_one_authentication() -> None:
    """The stronger version of the test above: forces genuine overlap (see
    `_SlowTransport`) and asserts on `max_in_flight` that the overlap
    actually happened, so this test cannot silently stop testing anything
    the way its plain-`MockTransport` sibling can. This is the one that
    fails when the single-flight lock is deleted."""
    server = FakeEmbyServer()
    transport = SlowTransport(server.handle)
    client = httpx.AsyncClient(transport=transport, base_url="https://emby.invalid")
    session = EmbySession(
        client,
        CREDENTIALS,
        source_name="Living Room Emby",
        device_id=DEVICE_ID,
        app_version="0.1.0",
    )
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await asyncio.gather(
            *(session.json_body("GET", SYSTEM_INFO_PATH, op="info") for _ in range(8))
        )
    finally:
        await client.aclose()
    assert transport.max_in_flight >= 4, (
        f"test did not force real concurrency (max_in_flight={transport.max_in_flight}); "
        "not a meaningful run"
    )
    assert server.authentications == 2, (
        f"SINGLE-FLIGHT VIOLATED: {server.authentications} authentications for 8 "
        f"provably-concurrent 401s (max_in_flight={transport.max_in_flight})"
    )


async def test_wrong_credentials_raise_and_are_remembered() -> None:
    """Negative caching. Without it, five calls against a wrong password
    are five authentications, against an upstream measured at 1-5 s per
    request."""
    server = FakeEmbyServer()
    session, client = _session(
        server, credentials=SourceCredentials(username="usher", password=SecretStr("wrong"))
    )
    try:
        for _ in range(5):
            with pytest.raises(PortAuthFailed):
                await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 1


async def test_the_cooldown_expires_and_authentication_is_retried() -> None:
    """The other half of negative caching: a corrected password must not
    require a restart. Advances the injected clock rather than sleeping."""
    server = FakeEmbyServer()
    clock = _Clock()
    session, client = _session(
        server,
        credentials=SourceCredentials(username="usher", password=SecretStr("wrong")),
        clock=clock,
    )
    try:
        with pytest.raises(PortAuthFailed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        assert server.authentications == 1
        clock.now += 61.0
        with pytest.raises(PortAuthFailed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 2


async def test_a_rejected_credential_discards_the_dead_session_token() -> None:
    """`_authenticate_locked` clears `self._token` when Emby rejects the
    credentials, and the cost of not doing so only shows up *after* the
    cooldown expires: `_session()` would hand back a token minted before
    the password changed, so the first call of the recovered session is
    spent on a request that is already known to be doomed.

    Asserted as the exact request sequence, because the outcome is the
    same either way -- a retry does eventually recover. What differs is
    whether the fifth request is the re-authentication or another 401.
    """
    server = FakeEmbyServer()
    clock = _Clock()
    session, client = _session(server, clock=clock)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.reject_credentials()
        with pytest.raises(PortAuthFailed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        clock.now += 61.0
        server.credentials_valid = True
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.requests == [
        "POST /Users/AuthenticateByName",  # the first session
        "GET /System/Info",
        "GET /System/Info",  # 401: the session was invalidated
        "POST /Users/AuthenticateByName",  # 401: the password is wrong now
        # No fifth `GET /System/Info` with the dead token in front of it.
        "POST /Users/AuthenticateByName",
        "GET /System/Info",
    ]


async def test_the_anonymous_probe_carries_the_identity_but_no_session_token() -> None:
    """ "The whole reason `verify()` can separate 'unreachable' from 'bad
    credentials'". `/System/Info/Public` answers without authentication,
    so a failure there is a reachability failure and cannot be anything
    else -- which stops being true the moment this call authenticates
    first, because then a wrong password reports the source as
    *unreachable* rather than as reachable-with-bad-credentials, and the
    `SourceStatus` an operator reads names the wrong problem.

    The fake refuses a session token on this route for that reason, so
    routing this call through the authenticated helper fails here rather
    than passing.
    """
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        body = await session.anonymous_json(PUBLIC_INFO_PATH, op="verify_public")
    finally:
        await client.aclose()
    assert body["Version"] == SERVER_VERSION
    assert server.requests == ["GET /System/Info/Public"]
    assert server.authentications == 0
    assert server.tokens == [None]
    assert server.identities[0] is not None
    assert f'DeviceId="{DEVICE_ID}"' in server.identities[0]


async def test_the_anonymous_probe_reports_an_unreachable_server() -> None:
    """The failure this call exists to be able to report."""
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable):
            await session.anonymous_json(PUBLIC_INFO_PATH, op="verify_public")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, PortRateLimited), (503, PortUnavailable), (200, PortDataMalformed)],
)
async def test_the_anonymous_probe_translates_every_failure_shape(
    status: int, expected: type[Exception]
) -> None:
    """Same taxonomy as an authenticated call, minus the 401 handling it
    has no session to recover. The 200 case is a reverse proxy's HTML
    maintenance page, which is the realistic way this route lies."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status == 200:
            return httpx.Response(200, text="<html>maintenance</html>")
        return httpx.Response(status, headers={"retry-after": "12"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(expected):
            await session.anonymous_json(PUBLIC_INFO_PATH, op="verify_public")
    finally:
        await client.aclose()


async def test_user_id_authenticates_once_and_then_answers_from_the_session() -> None:
    """Emby's item and user-data routes all live under `/Users/{userId}/`,
    so `EmbyAdapter` asks for this before every walk, every `get_item`, and
    every write-back. An implementation that re-authenticated per call
    would turn one nightly reconcile into 94,395 authentications."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        first = await session.user_id()
        second = await session.user_id()
    finally:
        await client.aclose()
    assert first == USER_ID
    assert second == USER_ID
    assert server.authentications == 1


async def test_access_token_is_the_token_the_server_actually_accepts() -> None:
    """Used only to build direct-play URLs (ADR-0012). A token that is not
    the live session's is a playback link that 401s in the client's
    player, long after anything here could report it."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        token = await session.access_token()
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert token
    assert server.authentications == 1
    assert server.tokens[-1] == token


@pytest.mark.parametrize("call", ["user_id", "access_token"])
async def test_the_other_entry_points_also_refuse_to_run_after_aclose(call: str) -> None:
    """`_raise_if_closed` is on all three entry points, not just
    `request`: `EmbyAdapter._fetch` calls `user_id()` *before* it calls
    `request()`, so a check only on the latter lets a closed adapter
    authenticate against a live transport and succeed. The transport here
    is deliberately still open, which is the case an `httpx`-level check
    cannot cover."""
    server = FakeEmbyServer()
    session, client = _session(server)
    await session.aclose()
    try:
        with pytest.raises(PortUnavailable):
            await (session.user_id() if call == "user_id" else session.access_token())
    finally:
        await client.aclose()
    assert server.authentications == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, PortRateLimited), (500, PortUnavailable), (503, PortUnavailable)],
)
async def test_a_failing_authentication_endpoint_is_not_a_credential_failure(
    status: int, expected: type[Exception]
) -> None:
    """A 429 or a 5xx from `AuthenticateByName` says nothing about the
    password, so neither may become `PortAuthFailed` -- that is the one
    translation with a lasting side effect, since it arms the negative
    cache and refuses to try again for a minute. An Emby restarting behind
    a reverse proxy answers 502 to authentication for a few seconds;
    treating that as a wrong password would lock the source out of the
    reconcile that follows."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"retry-after": "12"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(expected):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        # Not remembered: the next call tries again rather than being
        # refused from the negative cache.
        with pytest.raises(expected):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_a_transport_error_becomes_port_unavailable() -> None:
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        # Not an `httpx.HTTPError`. `StreamError` subclasses `RuntimeError`
        # instead, and `InvalidURL`/`CookieConflict` subclass `Exception`
        # directly -- all three verified against httpx's own hierarchy.
        httpx.StreamError("the stream went away"),
        httpx.InvalidURL("that is not a URL"),
        httpx.CookieConflict("two cookies of that name"),
    ],
)
async def test_a_transport_failure_outside_httpx_httperror_still_becomes_a_port_error(
    failure: Exception,
) -> None:
    """`except httpx.HTTPError` is not the whole surface, and the gap is not
    theoretical: `usher.ports.source` requires every method on this port to
    fail through `usher.ports.errors`, because that taxonomy is the only
    thing a caller can catch. An `httpx.StreamError` escaping as itself
    reaches PRD 03's reconciler as an exception it has never heard of.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_an_injected_client_closed_by_its_owner_becomes_a_port_error() -> None:
    """The exact hazard `usher.ports.source`'s `aclose` docstring records: a
    closed `httpx.AsyncClient` raises a bare `builtins.RuntimeError`, which
    is not an `httpx.HTTPError`.

    `EmbySession._raise_if_closed` covers the adapter closing *itself*. It
    cannot cover this, which is the other half of the configuration
    `test_aclose_closes_a_client_it_created_and_leaves_an_injected_one`
    exists to support: the client was injected, its owner closed it, and
    this session was never told.
    """
    server = FakeEmbyServer()
    session, client = _session(server)
    await client.aclose()
    with pytest.raises(PortUnavailable):
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(
                200,
                json={"AccessToken": "t", "User": {"Id": "u"}},
            )
        return httpx.Response(429, headers={"retry-after": "12"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortRateLimited) as exc_info:
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert exc_info.value.retry_after == 12.0


async def test_a_5xx_becomes_port_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_a_permanently_401ing_endpoint_retries_exactly_once_not_forever() -> None:
    """The gap none of the other tests close: every other 401 scenario in
    this file succeeds on the retry (an expired session re-mints a working
    one) or is stopped by the negative cache (authentication itself is
    rejected). Neither distinguishes "retried exactly once" from "retried
    N times" or even "retries forever", because the retry always either
    stops needing to happen or is blocked before it starts.

    This is the pathological case that actually exercises the bound: a
    server that happily authenticates (a fresh AccessToken every time, so
    the negative cache never engages) but whose protected endpoint 401s
    regardless of the token presented -- e.g. a session store the auth
    response never actually reaches. Without an explicit bound, "ask for a
    refresh and try again on a 401" is naturally recursive, and this is
    the test that would catch a refactor that turned it into one.
    """
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(f"{request.method} {request.url.path}")
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(
                200, json={"AccessToken": f"token-{len(request_log)}", "User": {"Id": "u"}}
            )
        return httpx.Response(401, json={"Error": "Access token is invalid or expired."})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortAuthFailed):
            await asyncio.wait_for(
                session.json_body("GET", SYSTEM_INFO_PATH, op="info"), timeout=5.0
            )
    finally:
        await client.aclose()
    protected_hits = sum(1 for r in request_log if r.endswith("/System/Info"))
    auth_hits = sum(1 for r in request_log if "AuthenticateByName" in r)
    assert protected_hits == 2, f"expected exactly one retry (2 attempts), got {protected_hits}"
    assert auth_hits == 2, f"expected exactly one refresh (2 authentications), got {auth_hits}"


async def test_a_non_json_body_becomes_port_data_malformed() -> None:
    """A reverse proxy serving an HTML error page with status 200 is the
    realistic case. A raw `json.JSONDecodeError` escaping here is not
    something any caller written against `usher.ports.errors` can catch."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(200, text="<html>maintenance</html>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_a_json_body_that_is_not_an_object_is_malformed() -> None:
    """`decode_json` promises a `dict`, and every caller indexes what it
    returns. A JSON array parses fine and then fails on the first `.get`
    with a `TypeError`, which is not an error any caller written against
    `usher.ports.errors` can catch."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(200, json=[{"Id": "not-an-object"}])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_an_authentication_response_without_a_token_is_malformed() -> None:
    """Distinguished from a 401 on purpose: a 200 with no AccessToken means
    something answered that is not Emby -- a captive portal, a proxy's
    landing page -- and retrying with the same credentials will not help."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Welcome": "to the hotel wifi"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("body", "missing"),
    [
        ({"User": {"Id": "u"}}, "no AccessToken"),
        ({"AccessToken": "t"}, "no User.Id"),
        ({"AccessToken": "t", "User": {"Id": ""}}, "no User.Id"),
        ({"AccessToken": "", "User": {"Id": "u"}}, "no AccessToken"),
    ],
)
async def test_each_half_of_the_authentication_response_is_validated_separately(
    body: dict[str, object], missing: str
) -> None:
    """The captive-portal case above is answered by *whichever* of the two
    checks runs first, so it holds with either one deleted -- each masks
    the other. These payloads are each valid but for one half, so each
    check has a case only it can answer.

    A 200 carrying a real token and no `User.Id` is the one that would
    otherwise go unguarded, and it is not hypothetical: every item route
    Emby offers is under `/Users/{userId}/`, so an empty user id builds
    `/Users//Items` and walks a library that is always empty -- a source
    that reports itself healthy and catalogues nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed, match=missing):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_no_error_message_ever_contains_the_password() -> None:
    """PRD 08: credentials are never logged, "including in error paths".
    Every message this class builds is interpolated from a method, a path,
    and a transport error -- none of which can carry the secret -- and the
    request body that does carry it is never formatted into one."""
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert "correct-horse-battery" not in str(exc_info.value)
    assert "correct-horse-battery" not in repr(exc_info.value)


async def test_no_credential_leaks_even_under_diagnose_true() -> None:
    """A stronger version of the test above, modelled on the real
    diagnose=True leak Group A found in usher.telemetry: that finding was
    a plaintext password rendered by loguru's frame-locals dump, not by
    any exception *message*. `configure_logging` hardcodes
    `diagnose=False` for exactly this reason, but this class must not
    depend on that global holding forever in every process that ever logs
    one of its exceptions -- so this asserts the stronger, local property
    directly: even with diagnose=True switched back on here, nothing
    _authenticate_locked touches (its `payload` dict holds the plaintext
    password as a local variable while `_send` is awaiting the network
    call) is rendered.

    This is a real property of `_send`'s shape, not a tautology: verified
    while writing this test that a version of `_send` calling
    `self._client.request(method, path, ..., json=payload, ...)` as one
    reference on the line that raises *does* leak under this exact probe
    -- loguru's diagnose renders the value of every name referenced on the
    exact source line an exception's frame reports, and `payload` was such
    a name. Splitting it into `build_request(...)` then `send(request)`
    means only `request` -- whose `__repr__` is method+URL, never a body
    -- is in scope on the line that can actually raise.
    """
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    sink = io.StringIO()
    logger.remove()
    try:
        logger.add(sink, diagnose=True, backtrace=True, level="ERROR")
        try:
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        except PortUnavailable as exc:
            try:
                raise exc
            except PortUnavailable:
                logger.exception("source request failed")
    finally:
        logger.remove()
        await client.aclose()
    assert "correct-horse-battery" not in sink.getvalue()


async def test_requests_after_aclose_raise_port_unavailable() -> None:
    """Verified while planning: a closed `httpx.AsyncClient` raises a bare
    `RuntimeError`, which is not an `httpx.HTTPError` -- so translation
    alone does not cover this and an explicit closed-flag does."""
    server = FakeEmbyServer()
    session, client = _session(server)
    await session.aclose()
    await client.aclose()
    with pytest.raises(PortUnavailable):
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")


async def test_every_upstream_request_produces_a_span() -> None:
    """Instrumentation is cross-cutting: "every subsequent milestone
    instruments its own work as it is built". PRD 10's span tree gets
    `source.request`, carrying the source and the operation so "why was
    this reconcile slow" is one query.

    Installs the in-memory exporter before the call, the same way
    tests/unit/test_telemetry.py does. The module-level tracer is a
    `ProxyTracer`, resolved lazily rather than at import -- but only
    *once*: it caches the first real provider it sees and never consults
    the global again. `tests/conftest.py`'s `reset_otel_tracer_provider`
    clears that cache around every test, which is what makes installing a
    provider here work regardless of what ran before. An earlier version
    of this docstring claimed the resolution happened per call; it does
    not, and this test failed for real once another test started reaching
    `EmbySession` under its own provider first.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    server = FakeEmbyServer()
    server.add_item(ITEM, T0)
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()

    spans = [span for span in exporter.get_finished_spans() if span.name == "source.request"]
    assert spans
    assert spans[0].attributes is not None
    assert spans[0].attributes["usher.op"] == "info"
    assert spans[0].attributes["usher.source"] == "Living Room Emby"


async def test_every_upstream_request_records_its_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`usher.source.request.duration` is PRD 10's catalogue entry for M3
    -- the milestone's one metric -- with the `source` and `op` labels
    that table specifies. Untested, replacing the `record` call with
    `pass` is invisible: nothing else in the suite observes it.

    Recorded in `_send`'s `finally`, so a request that fails at the
    transport is timed too. That is the case the metric is most wanted
    for: a source that has become slow enough to time out contributes
    nothing to a metric that only records successes.

    The clock advances here (see `_TickingClock`). Everywhere else in this
    file the injected clock is frozen, which makes every recorded duration
    exactly `0.0` -- an accepted consequence of one time source per
    session rather than a separate one for the cooldown and the metric,
    which could disagree in production and would be one more constructor
    knob to get wrong for a value only tests read.
    """
    recorder = _RecordingHistogram()
    monkeypatch.setattr(session_module, "_request_duration", recorder)
    server = FakeEmbyServer()
    session, client = _session(server, clock=_TickingClock(step=0.25))
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert [attributes["op"] for _, attributes in recorder.records] == ["authenticate", "info"]
    assert all(attributes["source"] == "Living Room Emby" for _, attributes in recorder.records)
    assert all(duration == pytest.approx(0.25) for duration, _ in recorder.records)


async def test_a_failed_request_is_timed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `finally`, specifically. A source that has started timing out
    is exactly the source an operator opens this metric to look at, and a
    `record` on the success path only would show it as having stopped
    making requests at all."""
    recorder = _RecordingHistogram()
    monkeypatch.setattr(session_module, "_request_duration", recorder)
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server, clock=_TickingClock(step=0.25))
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert [attributes["op"] for _, attributes in recorder.records] == ["authenticate"]
