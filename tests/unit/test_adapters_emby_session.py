"""EmbySession: the durable-client header, silent re-authentication, and
error translation. Driven entirely by httpx.MockTransport -- no network.
"""

import asyncio
import io
from datetime import UTC, datetime

import httpx
import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_server import FakeEmbyServer
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby.session import SYSTEM_INFO_PATH, EmbySession
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
    testable without a real sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _session(
    server: FakeEmbyServer,
    *,
    source_name: str = "Living Room Emby",
    credentials: SourceCredentials = CREDENTIALS,
    clock: _Clock | None = None,
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


async def test_a_transport_error_becomes_port_unavailable() -> None:
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


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
    tests/unit/test_telemetry.py does -- the module-level tracer is a
    ProxyTracer and resolves the global provider per call, so this works
    despite the module having been imported first.
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
