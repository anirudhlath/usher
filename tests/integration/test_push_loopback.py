"""`EmbyPushChannel` over a real `websockets` client and a real server.

**Why this is not a network test.** The server binds `127.0.0.1:0` and the
client connects to it. CLAUDE.md's socket guard blocks anything that is not
loopback and leaves loopback alone, which is the same allowance
`testcontainers` runs under -- and the guard is re-run as part of this
task's gate rather than trusted, because a green suite proves nothing about
a `sitecustomize.py` that was never on `PYTHONPATH`.

**What it closes that no unit test can.** `FakePushConnection` performs no
handshake: a wrong scheme, a missing `Upgrade` header, a proxy answering
404, a subprotocol mismatch, a real close code, a real ping/pong, real
`max_queue` backpressure, and the library's own logging are every one of
them invisible against it. All of those are real here, and the channel above
them -- URL construction, the subscribe frame, the decode, the ledger, the
translation of a closed socket -- is the real one too.

**Where this sits relative to the rest of the suite.**
`tests/unit/test_adapters_emby_push.py` already drives the real library on
loopback at the *connection* level (`connect_websocket` and
`_WebsocketsConnection`: a closed socket, a binary frame, a real deadline, a
failed send, and the credential half of the logging). It is kept there
because it needs no Docker. This file is the level above: `EmbyPushChannel`
itself, end to end over a real socket, which is the only place the URL that
goes on the wire, the frame that arrives at a peer, and a `SourceEvent` that
came out of real bytes are all the real thing at once.

**What it does not close.** This server is `websockets`, not Emby. It
accepts any path, answers any subscription, and sends whatever this file
tells it to -- so a wrong route, a wrong subscription frame, and a wrong
message envelope all pass here exactly as they pass everywhere else in this
repository. Only M5's live run closes those, and it is a named step of this
milestone.

**This file silences its own server, and that is a finding rather than
tidiness.** `websockets/server.py:561` logs `< GET %s HTTP/1.1` at DEBUG
with the request line, which for this channel carries `api_key=<token>`, and
`usher.telemetry.configure_logging` forces `propagate = True` on every
logger and installs a handler on root at level 0. Measured against this
harness: with the stock server logger, one handshake puts the token on
stdout **once** (the client's own copy is already silenced by
`connect_websocket`). A loopback file that silenced only the client would
fail on its own harness, and the obvious repair is to weaken the assertion
-- which is how a real leak gets ratified. `test_a_stock_server_logger_...`
below is that measurement, kept as a case so the fixture's `logger=` cannot
be "cleaned up".
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from loguru import logger as loguru_logger
from websockets.asyncio.server import ServerConnection, serve

from usher.adapters.emby.push import (
    SUBSCRIBE_FRAME,
    WEBSOCKET_PATH,
    EmbyPushChannel,
    PushHealth,
    connect_websocket,
    socket_logger,
)
from usher.config import Settings
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceEvent, SourceEventKind
from usher.telemetry import configure_logging

# The token this file looks for on stdout. A distinctive literal rather than
# a realistic one: what is being asserted is that a *known* string never
# reaches a stream, so it has to be findable.
TOKEN = "session-token-1"
DEVICE_ID = "device-1"
# Every await in this file is bounded. The failure mode of a socket test is a
# hang, not a wrong answer -- and a hang in a sweep reads as a mutation
# nothing observed rather than as one everything caught.
BOUND = 5.0
# Fast enough that a case waiting on a tick is a test rather than a wait, and
# it is the value the real connection hands `asyncio.wait_for`, so the poll
# loop really does cancel and re-issue `recv()` several times per case.
POLL_SECONDS = 0.05
# Ids in the reserved synthetic band (>= 90,000,000) --
# `tests/unit/test_no_third_party_data.py` scans this file too.
FIRST_ITEM_ID = 90_000_100


class _StubSession:
    """`SessionLike`, without an httpx client or a credential row.

    The channel asks a session for exactly two things and this is both of
    them. Structural, because `SessionLike` is a `Protocol` -- see
    `usher.adapters.emby.push` for why that one is a protocol and
    `PushConnection` beside it is an ABC.
    """

    def __init__(self, token: str = TOKEN, user: str = "user-1") -> None:
        self._token = token
        self._user = user

    async def access_token(self) -> str:
        return self._token

    async def user_id(self) -> str:
        return self._user


class _Server:
    """A real WebSocket server that records what a client sent it."""

    def __init__(self) -> None:
        self.base_url = ""
        self.paths: list[str] = []
        self.received: list[str] = []
        self.to_send: list[str] = []
        self.connections: list[ServerConnection] = []
        self.close_after_subscribe = False

    async def handle(self, connection: ServerConnection) -> None:
        self.connections.append(connection)
        self.paths.append(connection.request.path if connection.request else "<no request>")
        subscription = await connection.recv()
        self.received.append(
            subscription if isinstance(subscription, str) else subscription.decode()
        )
        if self.close_after_subscribe:
            await connection.close(code=1011, reason="upstream error")
            return
        for frame in self.to_send:
            await connection.send(frame)
        # Held open rather than returning: a handler that returns closes the
        # connection, which would make every case below race a close it did
        # not ask for.
        await connection.wait_closed()


@pytest_asyncio.fixture
async def server() -> AsyncIterator[_Server]:
    """A real server on `127.0.0.1:0`, with its own logger silenced.

    `logger=socket_logger()` is not tidiness -- see the module docstring and
    the case that measures what happens without it.
    """
    handler = _Server()
    async with serve(handler.handle, "127.0.0.1", 0, logger=socket_logger()) as running:
        host, port = running.sockets[0].getsockname()[:2]
        handler.base_url = f"http://{host}:{port}"
        yield handler


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Undo what `configure_logging` does to the whole process.

    It removes every loguru sink, installs one on `sys.stdout`, clears
    `handlers` and forces `propagate = True` on every logger in
    `loggerDict`, and puts an intercept handler on root at level 0. A test
    that ran it and walked away would leave the rest of the session logging
    through a sink bound to *this* test's captured stdout. Same fixture
    `tests/unit/test_adapters_emby_push.py` uses, for the same reason.
    """
    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level
    try:
        yield
    finally:
        loguru_logger.remove()
        loguru_logger.configure(patcher=None)
        logging.root.handlers = root_handlers
        logging.root.setLevel(root_level)


def _channel(base_url: str, *, stale_after: float = 90.0) -> EmbyPushChannel:
    return EmbyPushChannel(
        _StubSession(),
        base_url=base_url,
        device_id=DEVICE_ID,
        health=PushHealth(stale_after=stale_after),
        # `proxy=None` on the real connector. `websockets` 16 resolves the
        # proxy from the environment by default (`get_proxy` ->
        # `urllib.request.getproxies()`), and `urllib`'s `proxy_bypass` does
        # **not** exempt loopback unless `no_proxy` says so -- so a developer
        # machine with `HTTP_PROXY` set would send this connection through
        # it and every case in this file would fail somewhere unrelated. The
        # connector's default is unchanged: a household behind a proxy is a
        # real deployment and the env-var default serves it.
        connect=lambda url: connect_websocket(url, proxy=None),
        poll_seconds=POLL_SECONDS,
    )


async def _until(predicate: Callable[[], bool], *, what: str, bound: float = BOUND) -> None:
    deadline = time.perf_counter() + bound
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


@asynccontextmanager
async def _consuming(channel: EmbyPushChannel) -> AsyncIterator[list[SourceEvent]]:
    """Open the channel and drain it in a task, as a lane does.

    Nothing reads the socket until somebody calls `__anext__` -- `_events`
    is an async generator -- so a case that opened the channel and waited
    would measure a socket nobody was reading. This is the consumer, and it
    is a separate task so the case itself can wait on the ledger.
    """
    received: list[SourceEvent] = []

    async def pump(events: AsyncIterator[SourceEvent]) -> None:
        async for event in events:
            received.append(event)

    async with channel.open() as events:
        task = asyncio.create_task(pump(events))
        try:
            yield received
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _library_changed(*external_ids: str) -> str:
    return json.dumps({"MessageType": "LibraryChanged", "Data": {"ItemsAdded": [*external_ids]}})


_SESSIONS = json.dumps({"MessageType": "Sessions", "Data": []})


async def test_a_real_handshake_carries_the_token_and_the_device_id(server: _Server) -> None:
    """The request line a real client puts on a real wire.

    `FakePushConnector` records the URL string it was handed, so every unit
    case about this URL is really a case about `_socket_url`'s return value.
    Here the assertion is on what the *server* parsed out of the HTTP
    request: the scheme was accepted, the path survived, and the query
    string reached the peer intact.
    """
    server.to_send = [_SESSIONS]
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(
            lambda: channel.health.messages_received >= 1, what="the first frame to arrive"
        )
    assert server.paths == [f"{WEBSOCKET_PATH}?api_key={TOKEN}&deviceId={DEVICE_ID}"]


async def test_the_subscription_frame_arrives_verbatim(server: _Server) -> None:
    """ADR-0004's own frame, byte for byte, as a real text frame.

    Without it Emby holds the socket open and sends nothing -- which is
    indistinguishable from every other upgraded-but-silent failure this
    module exists to detect. The fake records a string in a list; this
    records what came out of a real WebSocket frame on the other end.
    """
    server.to_send = [_SESSIONS]
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(lambda: bool(server.received), what="the subscription to reach the server")
    assert server.received == [SUBSCRIBE_FRAME]
    assert json.loads(server.received[0]) == {"MessageType": "SessionsStart", "Data": "0,1000"}


async def test_a_real_message_becomes_a_real_event(server: _Server) -> None:
    """Bytes on a socket into a `SourceEvent`, with nothing faked in
    between, and the ledger moving because a frame really arrived."""
    server.to_send = [_library_changed(str(FIRST_ITEM_ID))]
    channel = _channel(server.base_url)
    async with _consuming(channel) as received:
        await _until(lambda: bool(received), what="an event to be yielded")
    assert [event.kind for event in received] == [SourceEventKind.ITEM_ADDED]
    assert received[0].external_ids == (str(FIRST_ITEM_ID),)
    assert channel.health.messages_received == 1
    assert channel.health.events_emitted == 1
    assert channel.health.is_delivering(now=time.monotonic()) is False, (
        "the ledger keeps answering after the channel closed"
    )


async def test_the_ledger_reports_delivering_only_once_a_real_frame_has_arrived(
    server: _Server,
) -> None:
    """The milestone's central rule, over a socket that really upgraded.

    The handshake succeeds and `connected` is true the instant it does, so
    an implementation reading health off the connection object answers
    `True` here -- against a peer that has said nothing. Only the message
    clause makes that read `False`.

    **And the silence is long enough to matter**, which makes this the push
    lane's answer to the defect `GET /events` had: several `poll_seconds`
    elapse before the frame is sent, so the channel's `asyncio.wait_for`
    around `recv()` has cancelled and re-issued a pending receive at least
    three times, and the message that arrives afterwards still arrives.
    `websockets` documents cancelling `recv` as safe ("the next invocation
    will return the next message") and a coroutine method is not an async
    generator's `__anext__`, so the failure the SSE route had is not
    reachable here -- but that is an argument, and this is the measurement.
    """
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(lambda: bool(server.received), what="the handshake to complete")
        assert channel.health.connected is True
        assert channel.health.is_delivering(now=time.monotonic()) is False
        await asyncio.sleep(POLL_SECONDS * 3)
        assert channel.health.messages_received == 0, "the poll ticks counted as messages"
        await server.connections[0].send(_SESSIONS)
        await _until(lambda: channel.health.messages_received >= 1, what="a frame")
        assert channel.health.is_delivering(now=time.monotonic()) is True


async def test_every_frame_sent_while_the_lane_was_not_reading_is_still_delivered(
    server: _Server,
) -> None:
    """The premise `PushSupervisor`'s connect-then-walk ordering rests on:
    a real socket loses nothing while the lane is somewhere else.

    The supervisor runs its gap-closing delta reconcile **after**
    connecting, with the socket already live, precisely so a change made
    during the walk is buffered rather than missed. That is a claim about a
    transport, and `FakePushConnection`'s unbounded in-process queue cannot
    fail it -- here the frames go through a real receive buffer, real
    framing and real `permessage-deflate`, and the assertion is on all 300
    arriving **in order**.

    **What this case does not prove, measured rather than assumed:
    `max_queue=256`.** Reverting it to the library's default of 16 leaves
    this case green. Nothing is *lost* at either setting -- 300 small frames
    fit in the socket buffers long before the client's queue matters -- so
    the difference between the two is buffered latency and memory, not
    correctness, and no assertion at a volume a test suite can afford
    distinguishes them. `connect_websocket`'s own docstring is where that
    number's reason lives; this case pins the property the number exists to
    protect.
    """
    count = 300
    channel = _channel(server.base_url)
    received: list[SourceEvent] = []
    async with channel.open() as events:
        await _until(lambda: bool(server.received), what="the handshake to complete")
        connection = server.connections[0]
        for index in range(count):
            await connection.send(_library_changed(str(FIRST_ITEM_ID + index)))
        # The gap-closing walk, in the one respect this case cares about:
        # time spent not reading the socket.
        await asyncio.sleep(0.1)
        iterator = aiter(events)
        for _ in range(count):
            received.append(await asyncio.wait_for(anext(iterator), timeout=BOUND))
    assert [event.external_ids[0] for event in received] == [
        str(FIRST_ITEM_ID + index) for index in range(count)
    ]
    assert channel.health.messages_received == count


async def test_a_real_server_close_raises_port_unavailable_out_of_the_iterator(
    server: _Server,
) -> None:
    """A real close frame with a real code, translated, raised out of the
    channel's own `async for`.

    The unit suite arranges this with `FakePushConnection.drop`, which
    raises the exception the wrapper is *supposed* to produce -- so it can
    only ever prove the channel propagates one, never that a real
    `ConnectionClosedError` becomes one. And the message names a path and an
    exception type, never a URL: `websockets`' own exceptions carry the URI
    on some paths and this URI carries the session token.
    """
    server.close_after_subscribe = True
    channel = _channel(server.base_url)
    async with channel.open() as events:
        with pytest.raises(PortUnavailable) as caught:
            await asyncio.wait_for(anext(aiter(events)), timeout=BOUND)
    message = str(caught.value)
    assert TOKEN not in message
    assert "api_key" not in message
    assert WEBSOCKET_PATH in message
    assert channel.health.connected is False


async def test_a_real_ping_is_answered_while_the_poll_loop_is_cancelling_recv(
    server: _Server,
) -> None:
    """A real ping, a real pong, through a poll loop that keeps cancelling
    the receive underneath it.

    This channel polls by wrapping `recv()` in `asyncio.wait_for`, so it
    **cancels a pending receive several times a second** for the life of the
    connection. `websockets` documents cancelling `recv` as safe ("there's
    no risk of losing data"); this is that documentation measured against
    the loop that depends on it, on the control-frame path rather than the
    data path -- a keepalive that stopped being answered under a polling
    consumer is a connection nginx closes at 60 s.

    **It is not the assertion that pins PRD 03's heartbeat interval, and
    that is measured rather than assumed.** `ping_interval=None` in
    `connect_websocket` leaves this case green -- correctly, because a peer
    answers pings whatever its own interval is. The requirement ("Emby sends
    no keepalive of its own... the client must generate traffic") is pinned
    by `tests/unit/test_adapters_emby_push.py::
    test_connect_websocket_hands_the_library_the_silenced_logger`, which
    asserts the two constants where they are passed. The two cases are
    layered; neither substitutes for the other.
    """
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(lambda: bool(server.received), what="the handshake to complete")
        connection = server.connections[0]
        # Long enough that the poll loop has timed out and re-issued `recv`
        # at least twice before the ping goes out.
        await asyncio.sleep(POLL_SECONDS * 3)
        pong = await connection.ping()
        await asyncio.wait_for(pong, timeout=BOUND)
        assert connection.latency > 0


async def test_a_failed_connection_names_no_url(server: _Server) -> None:
    """`websockets.exceptions.InvalidURI.__str__` contains the URI, and this
    URI contains the token. Arranged against a port nothing is listening on,
    which is a real `OSError` out of the real connector rather than a fake's
    ready-made `PortUnavailable`.

    Port 1 on loopback: reserved, never bound, and refused immediately, so
    this costs nothing and waits for nothing.
    """
    channel = _channel("http://127.0.0.1:1")
    with pytest.raises(PortUnavailable) as caught:
        async with channel.open():
            pass  # pragma: no cover -- the connect above never returns
    message = str(caught.value)
    assert TOKEN not in message
    assert "api_key" not in message
    assert "127.0.0.1" not in message
    assert WEBSOCKET_PATH in message
    assert channel.health.connected is False
    assert channel.health.opened_at is None, "a failed connect recorded an open"


async def test_a_proxy_in_the_environment_does_not_capture_a_loopback_connection(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`connect_websocket(proxy=None)`, and the proof that the argument is
    doing something.

    `websockets` 16 resolves a proxy from the environment by default, and
    `urllib.request.proxy_bypass` does **not** exempt loopback unless
    `no_proxy` names it -- so on a developer machine with `HTTP_PROXY` set,
    every case in this file would be dialling a proxy. The fixture passes
    `proxy=None`; this is the case that makes that argument observable
    instead of decorative.

    **Both halves, or this proves nothing.** The environment is poisoned
    with a proxy on a port nothing is listening on, and the second half
    asserts that the *default* connector really does try to use it. Without
    that, a `getproxies()` that silently ignored the variable would leave
    the first half passing against a `proxy=None` that had been deleted --
    the same trap as a `sitecustomize.py` that is not on `PYTHONPATH`.
    """
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")

    server.to_send = [_SESSIONS]
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(lambda: channel.health.messages_received >= 1, what="a frame")

    captured = EmbyPushChannel(
        _StubSession(),
        base_url=server.base_url,
        device_id=DEVICE_ID,
        health=PushHealth(stale_after=90.0),
        connect=connect_websocket,
        poll_seconds=POLL_SECONDS,
    )
    with pytest.raises(PortUnavailable):
        async with captured.open():
            pass  # pragma: no cover -- the connect above never returns


async def test_the_channel_cannot_log_the_token_at_debug(
    server: _Server, capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """The leak, closed and proved against the real library through the real
    channel.

    `websockets/client.py:294` debug-logs the request line, which for this
    channel carries `api_key=`, and `configure_logging` forces every logger
    to propagate into a handler on root at level 0 -- so `USHER_LOG_LEVEL=
    DEBUG`, the level an operator sets precisely when a source is
    misbehaving, is the configuration that would print it.

    `configure_logging` is called **inside** the test rather than in a
    fixture: loguru binds the stream object it is given at `add` time, so a
    sink installed before `capsys` replaced `sys.stdout` writes to the real
    one and this case would capture an empty string and pass against
    anything.
    """
    configure_logging(
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            secret_key="0123456789abcdef0123456789abcdef",
            log_level="DEBUG",
            log_json=False,
        )
    )
    logging.getLogger().setLevel(logging.DEBUG)
    server.to_send = [_SESSIONS]
    channel = _channel(server.base_url)
    async with _consuming(channel):
        await _until(lambda: channel.health.messages_received >= 1, what="a frame")
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


async def test_a_stock_server_logger_leaks_the_token_from_the_harness_side(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """**Why the fixture above passes `logger=socket_logger()` to its own
    server**, pinned as a measurement rather than left as a comment.

    `websockets/server.py:561` is the mirror of the client's line and logs
    the same request line -- so a loopback file that silenced only the
    client fails on its own harness, at which point the tempting repair is
    to weaken the assertion in the case above. Measured here: exactly one
    occurrence, from the server, with `connect_websocket`'s own logger
    already silenced.

    If this case ever *stops* leaking, the library has changed and the
    fixture's `logger=` argument should be re-read rather than deleted.
    """

    async def handler(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(_SESSIONS)
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as running:
        port = running.sockets[0].getsockname()[1]
        configure_logging(
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                secret_key="0123456789abcdef0123456789abcdef",
                log_level="DEBUG",
                log_json=False,
            )
        )
        logging.getLogger().setLevel(logging.DEBUG)
        channel = _channel(f"http://127.0.0.1:{port}")
        async with _consuming(channel):
            await _until(lambda: channel.health.messages_received >= 1, what="a frame")
    captured = capsys.readouterr()
    assert captured.out.count(TOKEN) == 1, (
        "the stock server logger no longer prints the request line; re-read the "
        "fixture's logger= argument rather than removing it"
    )
