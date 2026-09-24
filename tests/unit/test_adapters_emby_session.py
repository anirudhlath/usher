"""EmbySession: the durable-client header, silent re-authentication, and error translation.

Driven entirely by httpx.MockTransport -- no network.
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
from usher.adapters.emby.session import (
    AUTHENTICATE_PATH,
    PUBLIC_INFO_PATH,
    SYSTEM_INFO_PATH,
    EmbySession,
    redact_path,
)
from usher.adapters.http import _MinInterval
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
    """An injected monotonic clock, so the cooldown's expiry needs no real sleep.

    Frozen: `now` only moves when a test moves it. The same clock times
    `usher.source.request.duration`, one time source per session rather than two knobs
    that can disagree, so every duration recorded under it is exactly `0.0`.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _TickingClock:
    """A monotonic clock advancing by `step` per read, so elapsed time is non-zero."""

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
    """PRD 03's `Authorization: MediaBrowser Client="Usher", Device=…, DeviceId=…, Version=…`.

    The fake rejects an authentication without it, so this fails loudly rather than
    subtly if the header is dropped.
    """
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.device_ids == [DEVICE_ID]
    assert server.devices == ["Living Room Emby"]


async def test_the_identity_header_rides_on_every_request_not_just_authentication() -> None:
    """Every request carries `Authorization`, not just the authenticating one.

    Emby attributes traffic to a device per *request*, so a header sent only to
    `AuthenticateByName` mints one correctly-named session and files every subsequent
    call under an anonymous client. The fake rejects any request without it on every
    route, so dropping it from `_headers()` fails loudly here.
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
    """The durable-client invariant, and why `device_id` is persisted on the `Source`.

    A new id per authentication makes Usher an accumulating pile of sessions in Emby's
    dashboard.
    """
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
    """`My "Home" Emby` is a name an operator can type straight into `POST /admin/sources`.

    Interpolated raw it closes the quoted field early and Emby parses the header as
    something else entirely.
    """
    server = FakeEmbyServer()
    session, client = _session(server, source_name='My "Home" Emby')
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.devices == ["My _Home_ Emby"]


async def test_an_expired_session_is_silently_re_minted() -> None:
    """A token that starts returning 401 is renewed without a human pasting one."""
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
    """Single flight: eight requests hitting an expired session mint one, not eight."""
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
    """The stronger version of the case above: the overlap is forced and asserted.

    `_SlowTransport` makes the requests genuinely concurrent and `max_in_flight` proves
    it, so this cannot silently stop testing anything. It is the one that fails when
    the single-flight lock is deleted.
    """
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
    """Negative caching: a wrong password is not re-tried on every call.

    Without it, five calls against a wrong password are five authentications against a
    slow upstream.
    """
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
    """The other half of negative caching: a corrected password must not require a restart.

    Advances the injected clock rather than sleeping.
    """
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
    """`_authenticate_locked` clears `self._token` when Emby rejects the credentials.

    Not doing so only shows up after the cooldown expires: `_session()` would hand back
    a token minted before the password changed, spending the first call of the
    recovered session on a request already known to be doomed. Asserted as the exact
    request sequence, because a retry recovers either way.
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
    """`/System/Info/Public` is called unauthenticated, which is what makes it a probe.

    A failure there is a reachability failure and cannot be anything else -- until the
    call authenticates first, at which point a wrong password reports the source as
    unreachable and the `SourceStatus` an operator reads names the wrong problem. The
    fake refuses a session token on this route for that reason.
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
    """Same taxonomy as an authenticated call, minus the 401 it has no session for.

    The 200 case is a reverse proxy's HTML maintenance page, which is the realistic way
    this route lies.
    """

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
    """The user id is cached, because every item and user-data route needs it.

    `EmbyAdapter` asks for it before every walk, every `get_item` and every write-back,
    so re-authenticating per call would turn one nightly reconcile into an
    authentication per item.
    """
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
    """The token handed out for direct-play URLs is the live session's.

    Any other is a playback link that 401s in the client's player, long after anything
    here could report it.
    """
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
    """`_raise_if_closed` is on all three entry points, not just `request`.

    `EmbyAdapter._fetch` calls `user_id()` *before* it calls `request()`, so a check
    only on the latter lets a closed adapter authenticate against a live transport and
    succeed.

    The transport here is deliberately still open, which is the case an `httpx`-level
    check cannot cover.
    """
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
    """A 429 or a 5xx from `AuthenticateByName` says nothing about the password.

    Neither may become `PortAuthFailed`, the one translation with a lasting side effect:
    it arms the negative cache. An Emby restarting behind a reverse proxy answers 502
    for a few seconds, and treating that as a wrong password locks the source out of
    the reconcile that follows.
    """

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
        # Not an `httpx.HTTPError`: `StreamError` subclasses `RuntimeError`
        # instead, and `InvalidURL`/`CookieConflict` subclass `Exception`
        # directly.
        httpx.StreamError("the stream went away"),
        httpx.InvalidURL("that is not a URL"),
        httpx.CookieConflict("two cookies of that name"),
    ],
)
async def test_a_transport_failure_outside_httpx_httperror_still_becomes_a_port_error(
    failure: Exception,
) -> None:
    """`except httpx.HTTPError` is not the whole surface.

    Every method on this port has to fail through `usher.ports.errors`, the only
    taxonomy a caller can catch; an `httpx.StreamError` escaping as itself reaches the
    reconciler as an exception it has never heard of.
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


def _raising_client(failure: BaseException, *, timeout: float = 30.0) -> httpx.AsyncClient:
    """A client whose transport authenticates and then raises `failure`.

    The authenticate arm has to succeed, or every message under test reads
    `POST /Users/AuthenticateByName failed: …` and the path in it is a
    constant rather than the interpolated one the defect was found on.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": USER_ID}})
        raise failure

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://emby.invalid",
        timeout=timeout,
    )


@pytest.mark.parametrize(
    "failure",
    [
        # Constructed the way httpcore and httpx actually raise them.
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.PoolTimeout(""),
        httpx.WriteTimeout(""),
        httpx.ReadError(""),
        httpx.WriteError(""),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_a_transport_failure_that_stringifies_empty_still_names_itself(
    failure: BaseException,
) -> None:
    """The message names the exception type, because `str(exc)` is often empty.

    Every one of these transport errors stringifies to the empty string, so a message
    built from `str(exc)` alone ends at its colon and an operator cannot tell a read
    timeout from a connect failure from a pool exhaustion. `type(exc).__name__` is
    non-empty by construction, and is what the other adapters spell at this arm.
    """
    client = _raising_client(failure)
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", f"/Users/{USER_ID}/Items", op="list")
    finally:
        await client.aclose()
    message = str(exc_info.value)
    assert type(failure).__name__ in message
    # The premise: these stringify to the empty string, so a message built
    # from `str(exc)` alone would end at its colon.
    assert not message.rstrip().endswith(":")
    assert message.split("failed:", 1)[1].strip()


async def test_a_timeout_carries_the_budget_it_exhausted() -> None:
    """`ReadTimeout` says which phase gave up; the budget says what it was.

    That is the question an operator reading `sync_runs.error` is asking: whether to
    raise `USHER_SOURCE_TIMEOUT_SECONDS` or go look at the network. The number is
    recovered rather than invented -- httpx writes `extensions["timeout"]` on the
    request and sets `.request` on every `RequestError`.
    """
    client = _raising_client(httpx.ReadTimeout(""), timeout=7.5)
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", f"/Users/{USER_ID}/Items", op="list")
    finally:
        await client.aclose()
    assert "7.5s" in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [
        # No `.request` at all: `CookieConflict` and `InvalidURL` subclass
        # `Exception` directly, and a closed `httpx.AsyncClient` raises a
        # bare `builtins.RuntimeError`. Reading a timeout budget off these
        # must not raise *while formatting an exception message*, which
        # would replace a recorded sync failure with an unrelated crash.
        httpx.CookieConflict("two cookies of that name"),
        httpx.InvalidURL("that is not a URL"),
        RuntimeError("Cannot send a request, as the client has been closed."),
        # A `RequestError` whose `.request` was never set: `exc.request` is a
        # property that *raises* `RuntimeError` rather than answering `None`.
        httpx.ConnectError(""),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_a_failure_carrying_no_request_still_names_itself(
    failure: BaseException,
) -> None:
    client = _raising_client(failure)
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", f"/Users/{USER_ID}/Items", op="list")
    finally:
        await client.aclose()
    assert type(failure).__name__ in str(exc_info.value)


async def test_the_transport_failure_message_carries_no_credential() -> None:
    """Credentials are never logged, asserted on the message this arm builds.

    The control fires first: the password and the minted token are in scope at this
    call site, so a check that found nothing without one would be satisfied by a test
    that never held a secret.
    """
    secret = CREDENTIALS.password.get_secret_value()
    token = "a-minted-session-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            assert secret in request.content.decode()
            return httpx.Response(200, json={"AccessToken": token, "User": {"Id": USER_ID}})
        assert request.headers["X-Emby-Token"] == token
        raise httpx.ReadTimeout("")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", f"/Users/{USER_ID}/Items", op="list")
    finally:
        await client.aclose()
    message = str(exc_info.value)
    assert secret not in message
    assert token not in message
    assert "emby.invalid" not in message


async def test_an_injected_client_closed_by_its_owner_becomes_a_port_error() -> None:
    """A closed `httpx.AsyncClient` raises a bare `RuntimeError`, not an `HTTPError`.

    `EmbySession._raise_if_closed` covers the adapter closing itself; it cannot cover
    an injected client whose owner closed it without telling this session.
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
    """The retry is bounded, which no other 401 case in this file can see.

    Every other one either succeeds on the retry or is stopped by the negative cache,
    so none distinguishes "retried once" from "retries forever". This one authenticates
    happily every time while its protected endpoint 401s regardless of the token, which
    is the arrangement that turns "refresh and try again" into a recursion.
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
    """A reverse proxy serving an HTML error page with status 200 is the realistic case.

    A raw `json.JSONDecodeError` escaping here is not something any caller written
    against `usher.ports.errors` can catch.
    """

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
    """`decode_json` promises a `dict`, and every caller indexes what it returns.

    A JSON array parses fine and then fails on the first `.get` with a `TypeError`,
    which is not an error any caller written against `usher.ports.errors` can catch.
    """

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


async def test_a_deeply_nested_body_is_malformed_not_a_recursion_error() -> None:
    """A deeply nested body is a `RecursionError`, which is not a `ValueError`.

    `RecursionError` subclasses `RuntimeError`, so an `except ValueError` does not see
    it, and it is not a `UsherPortError` either -- it escapes the port and takes the
    worker down instead of parking one job. The body is whatever the server or a proxy
    put on the wire, and nothing here bounds its depth.

    The nesting below clears the interpreter's limit rather than sitting on it, because
    that limit is not a property this case has any business pinning.
    """
    depth = 12_000
    nested = ("[" * depth + "]" * depth).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(200, content=nested, headers={"content-type": "application/json"})

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
    """Distinguished from a 401 on purpose.

    A 200 with no AccessToken means something answered that is not Emby -- a captive
    portal, a proxy's landing page -- and retrying the same credentials will not help.
    """

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
    """Each half of the authentication payload has a case only it can answer.

    The captive-portal case above is satisfied by whichever check runs first, so it
    holds with either deleted. A 200 carrying a real token and no `User.Id` is the one
    that would otherwise go unguarded: an empty user id builds `/Users//Items` and
    walks a library that is always empty, so the source reports itself healthy and
    catalogues nothing.
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
    """Credentials are never logged, including in error paths.

    Every message this class builds is interpolated from a method, a path and a
    transport error -- none of which can carry the secret -- and the request body that
    does carry it is never formatted into one.
    """
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
    """The stronger version: a frame-locals dump must not render the password either.

    A `diagnose=True` traceback leaks through the locals, not through any exception
    message.
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
    """A closed `httpx.AsyncClient` raises a bare `RuntimeError`, not an `HTTPError`.

    Translation alone does not cover this; the explicit closed flag does.
    """
    server = FakeEmbyServer()
    session, client = _session(server)
    await session.aclose()
    await client.aclose()
    with pytest.raises(PortUnavailable):
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")


async def test_every_upstream_request_produces_a_span() -> None:
    """`source.request` carries the source and the operation, so one query answers why.

    The exporter is installed before the call. The module-level tracer is a
    `ProxyTracer` that caches the first real provider it sees and never consults the
    global again, so `reset_otel_tracer_provider` clearing that cache around every test
    is what makes installing a provider here work regardless of what ran before.
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
    """`usher.source.request.duration`, with the `source` and `op` labels PRD 10 lists.

    Untested, replacing the `record` call with `pass` is invisible, because nothing
    else in the suite observes it. Recorded in `_send`'s `finally`, so a request that
    fails at the transport is timed too -- the case the metric is most wanted for.

    The clock advances here; everywhere else in this file it is frozen, which is the
    accepted consequence of one time source per session.
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
    """The `finally`, specifically: a failed request is timed too.

    A source that has started timing out is exactly the source an operator opens this
    metric to look at, and recording on the success path alone would show it as having
    stopped making requests at all.
    """
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


class _GateClock:
    """A monotonic clock whose `sleep` is the only thing that moves it.

    Injected into the gate rather than into the session, because the limiter is handed
    in by the composition root: a case that wants a non-zero rate builds its own
    `_MinInterval` and gives it a clock it can drive, instead of reaching the real
    `asyncio.sleep`.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _CountingGate(_MinInterval):
    """A gate that counts acquisitions, so "which sends were paced" is a number."""

    def __init__(self, rate: float, *, source: str, clock: _GateClock) -> None:
        super().__init__(rate, source=source, clock=clock, sleep=clock.sleep)
        self.takes = 0

    async def take(self) -> None:
        self.takes += 1
        await super().take()


async def test_every_send_passes_the_gate_including_the_authenticating_one() -> None:
    """Every send pays the gate, counted rather than assumed.

    All four public entry points and `_authenticate_locked` reach the wire through
    `_send`, which is the only place `self._client` is touched, so the gate sits
    immediately above `build_request`.

    `_authenticate_locked` is the one easy to miss: it hangs off `_session()`, so a gate
    placed in `request()` would let it and `anonymous_json` through unthrottled -- and
    it is the send a wrong password turns into one extra request per call.

    The assertion is `takes == requests`, not `takes > 0`, which a gate on one send in
    five would satisfy.
    """
    server = FakeEmbyServer()
    clock = _GateClock()
    gate = _CountingGate(2.0, source="Living Room Emby", clock=clock)
    client = httpx.AsyncClient(transport=server.transport(), base_url="https://emby.invalid")
    session = EmbySession(
        client,
        CREDENTIALS,
        source_name="Living Room Emby",
        device_id=DEVICE_ID,
        app_version="0.1.0",
        reauth_cooldown_seconds=60.0,
        limiter=gate,
        clock=_Clock(),
    )
    try:
        # A fresh session, so this one call is the authenticating send plus
        # the caller's own -- the two that a gate on `request()` would count
        # as one.
        await session.request("GET", SYSTEM_INFO_PATH, op="info")
        await session.ok("GET", SYSTEM_INFO_PATH, op="info")
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        await session.anonymous_json(PUBLIC_INFO_PATH, op="verify")
        # Neither of these sends anything -- the session is authenticated by
        # now -- and that is the point: they are entry points too, so a
        # version of this case that called them first would be counting a
        # different five.
        assert await session.user_id() == USER_ID
        assert await session.access_token()
    finally:
        await client.aclose()

    assert f"POST {AUTHENTICATE_PATH}" in server.requests, (
        "the premise: the authenticating send happened, and it is the one this case is about"
    )
    assert len(server.requests) == 5, (
        f"the premise: five sends reached the wire -- {server.requests}"
    )
    assert gate.takes == len(server.requests), (
        f"{len(server.requests) - gate.takes} send(s) reached Emby without passing the gate: "
        f"{server.requests}"
    )
    # And the rate is real: 2 rps is one send every 0.5 s, and the first goes
    # immediately because the gate seeds `_next` to now rather than to the
    # past. A gate that never slept would be a knob that reads config and
    # paces nothing.
    assert clock.slept == [0.5, 0.5, 0.5, 0.5], (
        f"five sends at 2 rps is four half-second waits, not {clock.slept}"
    )


# --- the redacted request path ---------------------------------------------

REAL_USER = "f106b04c6e9f497a846a94aa25703eed"


def _authenticating(then: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": REAL_USER}})
        if isinstance(then, httpx.Response):
            return then
        raise then  # type: ignore[misc]

    return httpx.MockTransport(handler)


def _session_over(transport: httpx.MockTransport) -> tuple[EmbySession, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport, base_url="https://emby.invalid")
    return (
        EmbySession(client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"),
        client,
    )


@pytest.mark.parametrize(
    ("answer", "raiser"),
    [
        (None, httpx.ReadTimeout("")),
        (httpx.Response(502), None),
        (httpx.Response(200, text="not json"), None),
    ],
    ids=["transport-failure", "5xx", "undecodable-body"],
)
async def test_no_raise_site_on_this_session_puts_a_user_id_in_its_message(
    answer: httpx.Response | None, raiser: BaseException | None
) -> None:
    """Redaction is scoped to the session rather than to one `raise`.

    `_send`, `ok` and `decode_json` each interpolate the path into a message, so a
    redaction applied at one of them leaves the other two leaking the identical id. The
    parametrisation is the control: three failure families, three raise sites, one path
    that really does carry a user id.
    """
    session, client = _session_over(_authenticating(answer if answer is not None else raiser))
    path = f"/Users/{REAL_USER}/Items"
    try:
        with pytest.raises((PortUnavailable, PortDataMalformed)) as exc_info:
            await session.json_body("GET", path, op="list")
    finally:
        await client.aclose()
    message = str(exc_info.value)
    assert REAL_USER not in message
    # Not collapsed to "a request failed": the route is the whole diagnostic
    # value and it survives.
    assert "/Users/{user_id}/Items" in message


async def test_the_rfc_9457_detail_is_redacted_too_because_it_reaches_a_client() -> None:
    """`decode_json` passes the path as both the message subject and the `detail`.

    `detail` is the half a route can put in an RFC 9457 body, so the message is a log
    line and this one is a response.
    """
    session, client = _session_over(_authenticating(httpx.Response(200, text="not json")))
    try:
        with pytest.raises(PortDataMalformed) as exc_info:
            await session.json_body("GET", f"/Users/{REAL_USER}/Items", op="list")
    finally:
        await client.aclose()
    detail = exc_info.value.detail
    assert detail is not None
    assert REAL_USER not in detail
    assert detail == "/Users/{user_id}/Items"


async def test_a_401_that_survives_reauthentication_names_the_route_not_the_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": REAL_USER}})
        return httpx.Response(401)

    session, client = _session_over(httpx.MockTransport(handler))
    try:
        with pytest.raises(PortAuthFailed) as exc_info:
            await session.json_body("GET", f"/Users/{REAL_USER}/Items", op="list")
    finally:
        await client.aclose()
    assert REAL_USER not in str(exc_info.value)


async def test_a_route_word_is_never_mistaken_for_an_identifier() -> None:
    """A route word in an id-shaped position is kept, not redacted.

    `/Users/AuthenticateByName` rendered as `/Users/{user_id}` would describe the one
    request that carries a password as an ordinary user read. `/System/Info/Public` is
    the same check for a two-word tail.
    """
    assert redact_path("/Users/AuthenticateByName") == "/Users/AuthenticateByName"
    assert redact_path("/System/Info/Public") == "/System/Info/Public"
    assert redact_path("/System/Info") == "/System/Info"


def test_redact_path_names_the_identifier_it_removed() -> None:
    """A placeholder rather than a blank.

    `/Users/{user_id}/Items/{item_id}` stays distinguishable from
    `/Users/{user_id}/Items`, which is what keeps this a redaction rather than a
    second blindfold.
    """
    assert redact_path(f"/Users/{REAL_USER}/Items") == "/Users/{user_id}/Items"
    assert redact_path(f"/Users/{REAL_USER}/Items/abc123") == "/Users/{user_id}/Items/{item_id}"
    assert (
        redact_path(f"/Users/{REAL_USER}/Items/abc123/UserData")
        == "/Users/{user_id}/Items/{item_id}/UserData"
    )
    assert (
        redact_path(f"/Users/{REAL_USER}/PlayedItems/abc")
        == "/Users/{user_id}/PlayedItems/{item_id}"
    )
    assert redact_path(f"/Users/{REAL_USER}") == "/Users/{user_id}"


def test_an_unrecognised_segment_is_redacted_rather_than_kept() -> None:
    """An unlearned segment is redacted, which is the safe direction.

    A route word this vocabulary has not learned renders as `{id}` -- a lost word in a
    message; the other default loses an identifier into a public issue.

    The route root is the one exception, on its own premise: every path this adapter
    issues begins with a route word, asserted below, so keeping it costs nothing and
    stops an unlearned route collapsing to something unreadable.
    """
    assert redact_path("/Sessions") == "/Sessions"
    # Deeper unlearned segments are lost, which is the cost being accepted.
    assert redact_path("/Sessions/9f2/Playing") == "/Sessions/{id}/{id}"


def test_no_path_this_adapter_issues_begins_with_an_identifier() -> None:
    """Every path this adapter issues begins with a route word.

    The route-root rule rests on it, and a premise stated only in prose is one refactor
    away from being false and silent.
    """
    for path in (
        AUTHENTICATE_PATH,
        PUBLIC_INFO_PATH,
        SYSTEM_INFO_PATH,
        f"/Users/{REAL_USER}",
        f"/Users/{REAL_USER}/Items",
        f"/Users/{REAL_USER}/Items/abc",
        f"/Users/{REAL_USER}/Items/abc/UserData",
        f"/Users/{REAL_USER}/PlayedItems/abc",
    ):
        assert path.split("/")[1] in {"Users", "System"}
