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
        # Constructed the way httpcore and httpx actually raise them, which
        # is the whole of this defect. `httpcore.map_exceptions` calls
        # `to_exc(exc)` with the *object* it caught -- a bare
        # `TimeoutError()` or an `anyio.EndOfStream()`, both of which
        # stringify to `""` -- and httpx's `map_httpcore_exceptions` then
        # re-raises with `message = str(exc)`. Measured on httpx 0.28.1
        # against real sockets: a server that accepts and never answers
        # gives `ReadTimeout` with `str(exc) == ""`; a blackholed address
        # gives `ConnectTimeout` with `str(exc) == ""`; an exhausted pool
        # gives `PoolTimeout` with `str(exc) == ""`.
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
    """Issue #35: a `watch_state` sync walked 121,000 items for 57 minutes,
    failed, and recorded `GET /Users/{id}/Items failed:` in `sync_runs.error`
    -- the whole diagnostic, ending at the colon.

    `str(exc)` was the entire payload and every one of these stringifies to
    the empty string, so the *common* path through this handler is the one
    that produces a message naming no failure at all. An operator cannot
    tell a read timeout from a connect failure from a pool exhaustion, and
    the run cost an hour.

    `type(exc).__name__` is non-empty by construction, which is exactly what
    `EmbyPushChannel`, `TmdbClient`, `OpenAICompatibleClient` and
    `TmdbImageProvider` already spell at the same arm.
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
    # The defect itself, asserted as its own premise: the reported message
    # ended at the colon with nothing after it.
    assert not message.rstrip().endswith(":")
    assert message.split("failed:", 1)[1].strip()


async def test_a_timeout_carries_the_budget_it_exhausted() -> None:
    """`ReadTimeout` says which phase gave up; the budget says *what it was*,
    which is the question the operator reading `sync_runs.error` is actually
    asking -- whether to raise `USHER_SOURCE_TIMEOUT_SECONDS` or go look at
    the network.

    Recoverable rather than invented: `Client.build_request` writes
    `extensions["timeout"]` from the client's own `Timeout`, and httpx sets
    `.request` on every `RequestError` on the way out, so the number is on
    the exception this handler already holds. Verified against httpx 0.28.1.
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
    """PRD 08's "credentials are never logged", asserted on the message this
    arm builds rather than assumed from the shape of it.

    The control fires first: the password and the minted token *are* in
    scope at this call site, so a check that found nothing without one would
    be satisfied by a test that never held a secret to begin with.
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


async def test_a_deeply_nested_body_is_malformed_not_a_recursion_error() -> None:
    """The defect M8 found and fixed in the LLM adapter, reaching this one --
    which is the point of `usher.adapters.http.decode_json` being one function
    rather than three copies.

    `json.loads` raises `RecursionError` past a nesting depth of 9,999, and
    `RecursionError` subclasses **`RuntimeError`, not `ValueError`**, so the
    `except ValueError` this adapter carried on its own did not see it. It is
    not a `UsherPortError` either, so it escaped the port entirely and took the
    worker process down instead of parking one job. Reachable here for the same
    reason as the HTML-error-page case above: the body is whatever the server,
    or a reverse proxy in front of it, put on the wire, and nothing this
    project controls bounds its depth.

    The depth is measured, not guessed -- 9,998 parses and 9,999 raises on
    CPython 3.13 at the default recursion limit -- and clears the boundary
    rather than sitting on it, because the boundary is an interpreter property
    this case has no business pinning.
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


class _GateClock:
    """A monotonic clock whose `sleep` is the only thing that moves it.

    Injected into the gate rather than into the session: after M10's S3 the
    limiter is **handed in** by the composition root rather than minted from a
    rate inside `EmbySession`, so a case that wants a non-zero rate builds its
    own `_MinInterval` and gives it a clock it can drive. Before S3 the session
    threaded `clock` into the gate and *not* `sleep`, which made a non-zero-rate
    session test call the real `asyncio.sleep` -- latent only because every Emby
    case used the `rate=0` default.
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
    """A gate that counts acquisitions, so *"which sends were paced"* is a
    number rather than an inference from a wall clock."""

    def __init__(self, rate: float, *, source: str, clock: _GateClock) -> None:
        super().__init__(rate, source=source, clock=clock, sleep=clock.sleep)
        self.takes = 0

    async def take(self) -> None:
        self.takes += 1
        await super().take()


async def test_every_send_passes_the_gate_including_the_authenticating_one() -> None:
    """`_send` is the whole of the Emby surface, and this counts rather than
    assumes it.

    All four public entry points -- `request`, `ok`, `json_body`,
    `anonymous_json` -- and `_authenticate_locked` reach the wire through
    `_send`, and `_send` is the only place `self._client` is touched. So the
    gate sits immediately above `build_request` and **every** send pays it.

    🔴 **`_authenticate_locked` is the one that is easy to miss**, because it
    is the only send that is not reached from a public method's own body: it
    hangs off `_session()`/`_refresh()`, so a gate placed in `request()` --
    the obvious spelling -- would let it and `anonymous_json` through
    unthrottled. It is also the send a *wrong* password turns into one extra
    request per call until the re-auth cooldown catches it
    (`Settings.source_reauth_cooldown_seconds`), i.e. exactly the traffic a
    courtesy limiter exists to space.

    The assertion is `takes == requests`, not `takes > 0`: a count that only
    has to be positive is satisfied by a gate on one send in five.
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
    """Issue #35, and the reason it is scoped to the session rather than to
    one `raise`: `_send`, `ok` and `decode_json` each interpolate the path
    into a message, and a redaction applied at only one of them leaves the
    other two leaking the identical id.

    The control is the parametrisation itself -- three different failure
    families reaching three different raise sites, all of them through one
    path that really does carry a user id.
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
    """`decode_json` passes the path as **both** the message subject and the
    `detail`, and `detail` is the half that a route can put in an RFC 9457
    body -- `SourceStatus.detail` is `str(exc)` on `GET /admin/sources/{id}
    /status` today. The message is a log line; this one is a response.
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
    """The failure direction that a redaction gets wrong quietly.
    `/Users/AuthenticateByName` has an id-shaped *position* holding a route
    word, and rendering it `/Users/{user_id}` would describe the one request
    that carries a password as if it were an ordinary user read.

    `/System/Info/Public` is the same check for a two-word tail.
    """
    assert redact_path("/Users/AuthenticateByName") == "/Users/AuthenticateByName"
    assert redact_path("/System/Info/Public") == "/System/Info/Public"
    assert redact_path("/System/Info") == "/System/Info"


def test_redact_path_names_the_identifier_it_removed() -> None:
    """A placeholder rather than a blank: `/Users/{user_id}/Items/{item_id}`
    is still distinguishable from `/Users/{user_id}/Items`, which is the
    property that keeps this a redaction rather than a second blindfold.
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
    """The safe direction, chosen deliberately and stated so it is not
    "fixed" later. A route word this vocabulary has not learned renders as
    `{id}` -- a lost *word* in a message. The other default loses an
    *identifier* into a public issue, which is what #35 cost.

    The route root is the one exception and it has its own premise: every
    path this adapter issues begins with a route word, asserted below, so
    keeping it costs nothing and is what stops an unlearned route from
    collapsing to something unreadable.
    """
    assert redact_path("/Sessions") == "/Sessions"
    # Deeper unlearned segments are lost, which is the cost being accepted.
    assert redact_path("/Sessions/9f2/Playing") == "/Sessions/{id}/{id}"


def test_no_path_this_adapter_issues_begins_with_an_identifier() -> None:
    """The premise the route-root rule rests on, asserted rather than
    assumed -- a rule whose premise is only stated in a docstring is one
    refactor away from being false and silent."""
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
