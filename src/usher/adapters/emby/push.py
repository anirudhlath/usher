"""Emby's WebSocket push channel (PRD 03)."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from loguru import logger

from usher.adapters.emby.mapping import library_ids, user_data_states
from usher.adapters.websocket import WebSocketConnection, silenced_logger
from usher.adapters.websocket import connect_websocket as _connect_websocket
from usher.ports.errors import PortUnavailable, UsherPortError
from usher.ports.source import SourceEvent, SourceEventKind

WEBSOCKET_PATH = "/embywebsocket"

# The subscription, verbatim: neither `Sessions` nor `UserDataChanged` arrives
# until this frame has been sent.
SUBSCRIBE_FRAME = '{"MessageType": "SessionsStart", "Data": "0,1000"}'

# How long one `recv` waits before reporting "nothing yet". A *tick*, not a
# timeout: each one runs the staleness watchdog. Small enough that a channel
# crossing `stale_after` is noticed within a few seconds of doing so, large
# enough that an idle lane is not spinning.
DEFAULT_POLL_SECONDS = 5.0

# How long a channel may deliver nothing at all before it is treated as dead. Nothing
# answers a subscribe and an idle library's frames come minutes apart, so this sits
# well above those gaps: every window that expires costs a reconnect.
DEFAULT_STALE_AFTER_SECONDS = 300.0

# The `websockets` client logs its own request line at DEBUG --
# `websockets/client.py:294`, `logger.debug("> GET %s HTTP/1.1", request.path)` -- and
# this channel's request path is `/embywebsocket?api_key=<token>&deviceId=<id>`.
_SOCKET_LOGGER_NAME = "usher.source.emby.socket"


def socket_logger() -> logging.Logger:
    """A logger `websockets` can write to and nothing can read from, for this channel."""
    return silenced_logger(_SOCKET_LOGGER_NAME)


@dataclass(slots=True)
class PushHealth:
    """What is known about a push channel, from messages rather than from a socket.

    Mutable on purpose: it is a ledger the channel writes and the adapter
    reads, and the adapter holds the *same* object across reconnects so
    `reconnects` and `messages_received` are cumulative for the lane rather
    than per connection.

    `stale_after` is a field rather than a parameter of `is_delivering`
    because two callers ask (the adapter's `supports_push`, and the
    channel's own watchdog) and a window they could pass differently is a
    window they eventually would.
    """

    stale_after: float
    connected: bool = False
    opened_at: float | None = None
    last_message_at: float | None = None
    messages_received: int = 0
    events_emitted: int = 0
    reconnects: int = 0

    def record_open(self, *, now: float) -> None:
        """A connection is up.

        Says nothing about whether it works.
        """
        if self.opened_at is not None:
            self.reconnects += 1
        self.connected = True
        self.opened_at = now
        self.last_message_at = None

    def record_close(self) -> None:
        self.connected = False

    def record_message(self, *, now: float) -> None:
        self.messages_received += 1
        self.last_message_at = now

    def record_event(self) -> None:
        self.events_emitted += 1

    def is_delivering(self, *, now: float) -> bool:
        """Whether this channel is a push channel a caller may rely on.

        **All three clauses, and the middle one is the milestone.** `connected`
        alone is `True` for a proxy that upgraded and buffers, for a NAT entry
        dropped while both ends still believe otherwise, and for a handshake
        against a path that does not exist. `messages_received > 0` is what
        those cannot satisfy.

        The staleness clause keeps the answer honest *after* the first message:
        a socket that delivered once an hour ago is not working, and
        `websockets`' `ping_interval`/`ping_timeout` cannot tell -- a peer
        answering pongs while delivering nothing passes the keepalive.
        """
        return (
            self.connected
            and self.messages_received > 0
            and self.last_message_at is not None
            and now - self.last_message_at <= self.stale_after
        )

    def silent_for(self, *, now: float) -> float:
        """Seconds since anything last arrived, or since the open when nothing has.

        Zero before a connection exists. That branch is unreachable from the
        loop that calls this, and it is spelled rather than left to a
        `None`-minus-`float` `TypeError` that would take a lane down.
        """
        since = self.last_message_at if self.last_message_at is not None else self.opened_at
        return 0.0 if since is None else now - since


#: Emby's name for the shared transport, whose contract `usher.adapters.websocket` states.
PushConnection = WebSocketConnection


_clock = time.monotonic


# Emby's `LibraryChanged` arrays, and the event each becomes.
_LIBRARY_ARRAYS: tuple[tuple[str, SourceEventKind], ...] = (
    ("ItemsAdded", SourceEventKind.ITEM_ADDED),
    ("ItemsUpdated", SourceEventKind.ITEM_UPDATED),
    ("ItemsRemoved", SourceEventKind.ITEM_REMOVED),
)


def to_source_events(
    message: Mapping[str, Any], *, source_user_id: str | None
) -> tuple[SourceEvent, ...]:
    """One decoded Emby message into zero or more `SourceEvent`s.

    **Never raises**, and every branch that could is spelled as a drop.
    There is no job behind this call and no caller that could park anything:
    a raise here reaches `PushSupervisor`'s `UsherPortError` arm, which
    reconnects and runs a gap-closing delta walk. Spending that on one
    malformed frame is worse than dropping the frame, and the nightly
    reconcile covers what a dropped frame would have carried.

    **A message that maps to nothing is still a message.** The periodic
    `Sessions` frame produces no event by design: deriving anything from it
    would mean tracking play sessions Usher never starts. Its value is that it
    arrives, and `EmbyPushChannel` counts every frame before consulting this. An
    unknown `MessageType` behaves identically, so a future Emby build's new
    message costs nothing rather than taking a lane down.
    """
    kind = message.get("MessageType")
    data = message.get("Data")
    if kind == "UserDataChanged":
        entries = data.get("UserDataList") if isinstance(data, Mapping) else None
        if not isinstance(entries, list):
            return ()
        ids, states = user_data_states(entries, source_user_id=source_user_id)
        if not ids:
            return ()
        return (
            SourceEvent(
                kind=SourceEventKind.WATCH_STATE_CHANGED,
                external_ids=tuple(ids),
                watch_states=tuple(states),
            ),
        )
    if kind == "LibraryChanged" and isinstance(data, Mapping):
        # `named`, not `ids`: the `UserDataChanged` branch above binds `ids`
        # to a `list[str]` in this same function scope, and a walrus reusing
        # the name is a mypy-strict error rather than a shadow.
        return tuple(
            SourceEvent(kind=event_kind, external_ids=named)
            for key, event_kind in _LIBRARY_ARRAYS
            if (named := library_ids(data.get(key)))
        )
    return ()


PushConnector = Callable[[str], Awaitable[PushConnection]]


class SessionLike(Protocol):
    """The two things this channel asks of an `EmbySession`.

    Named so the channel's own tests can substitute without constructing a
    session, an httpx client and a credential -- and so the dependency is *two
    methods* rather than "an `EmbySession`", which keeps a later reader from
    reaching for `request()` from inside a socket loop.

    A `Protocol` where `PushConnection` is an `ABC`, because the two seams
    differ in whether an implementation can inherit. `websockets`'
    `ClientConnection` cannot. `EmbySession` already has both methods in this
    same package, and making it inherit from here would have `session.py`
    import `push.py` -- the wrong direction, and one import from a cycle.
    """

    async def access_token(self) -> str: ...

    async def user_id(self) -> str: ...


class EmbyPushChannel:
    """One `/embywebsocket` connection, and the ledger that says whether it is working.

    Reuses `EmbySession` rather than authenticating: PRD 03's durable-client
    property comes from authenticating *once* with a stable `DeviceId`, and
    presenting an existing token alongside a different `DeviceId` neither forks
    nor invalidates the session -- Emby binds a session to the token's own
    authentication record. A channel that authenticated per reconnect would mint
    a session per reconnect and undo the one property the header exists for.
    Reusing it also inherits single-flight re-auth and the negative cache.
    """

    def __init__(
        self,
        session: SessionLike,
        *,
        base_url: str,
        device_id: str,
        health: PushHealth,
        connect: PushConnector,
        clock: Callable[[], float] = _clock,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._device_id = device_id
        self._health = health
        self._connect = connect
        self._clock = clock
        self._poll_seconds = poll_seconds

    @property
    def health(self) -> PushHealth:
        return self._health

    async def _socket_url(self) -> str:
        """`/embywebsocket?api_key=<token>&deviceId=<id>`, for the connector.

        **Never stored on the instance, never returned outside this module,
        never logged, never interpolated into an exception, never a span
        attribute** -- the second place this token is materialised. `quote` on
        both values because a device id is a persisted string an operator could
        have influenced, exactly as `EmbyAdapter._segment` argues for a path.

        The token is read from the session on **every** open rather than cached
        here, so a channel reconnecting after a silent re-authentication
        presents the new token instead of a revoked one forever.

        `http`/`https` become `ws`/`wss`, which `websockets` requires.
        """
        token = await self._session.access_token()
        parts = urlsplit(self._base_url.rstrip("/"))
        scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
        query = f"api_key={quote(token, safe='')}&deviceId={quote(self._device_id, safe='')}"
        return urlunsplit((scheme, parts.netloc, f"{parts.path}{WEBSOCKET_PATH}", query, ""))

    @asynccontextmanager
    async def open(self) -> AsyncIterator[AsyncIterator[SourceEvent]]:
        """Connect, subscribe, and yield the event stream.

        The connection is closed however the block ends -- a `finally`, not a
        trailing call, because the consumer is an `async for` a caller may
        `break` out of and because `PushSupervisor` wraps this in a
        `try/except` that catches its own iterator's raise. The `finally`
        starts *before* the subscribe frame is sent, so a peer that goes
        away during the handshake cannot leave a ledger reporting
        `connected` on a socket nobody is holding.
        """
        url = await self._socket_url()
        try:
            connection = await self._connect(url)
        except UsherPortError:
            # Already this port's vocabulary, and the only connector that
            # produces one is this project's own wrapper -- whose message
            # names a path and never a URL by construction. Re-wrapping it
            # would bury the reason behind a generic one.
            raise
        except Exception as exc:
            # A bare `except Exception` on purpose, with no suppression
            # directive: `BLE001` is not in this project's ruff selection, and
            # `RUF100` -- which is -- refuses a directive for a rule nothing
            # enables.
            raise PortUnavailable(
                f"{WEBSOCKET_PATH} could not be opened: {type(exc).__name__}"
            ) from exc
        self._health.record_open(now=self._clock())
        try:
            await connection.send(SUBSCRIBE_FRAME)
            yield self._events(connection)
        finally:
            self._health.record_close()
            await connection.aclose()

    async def _events(self, connection: PushConnection) -> AsyncIterator[SourceEvent]:
        source_user_id = await self._session.user_id()
        while True:
            await asyncio.sleep(0)
            try:
                frame = await connection.recv(self._poll_seconds)
            except TimeoutError:
                # A tick, not a failure -- and the tick is what runs the watchdog.
                self._raise_if_stale()
                continue
            # Counted **before** it is parsed and before it is mapped. A
            # frame is evidence the socket is alive whatever it says, and
            # counting only mapped events would make an idle library --
            # which is most libraries most of the time -- look dead.
            self._health.record_message(now=self._clock())
            for event in self._decode(frame, source_user_id):
                self._health.record_event()
                yield event

    def _raise_if_stale(self) -> None:
        """Raise `PortUnavailable` when nothing has arrived for `stale_after`."""
        silent = self._health.silent_for(now=self._clock())
        if silent <= self._health.stale_after:
            return
        raise PortUnavailable(
            f"{WEBSOCKET_PATH} delivered no message in {silent:.0f}s "
            f"(ceiling {self._health.stale_after:.0f}s); treating the channel as dead"
        )

    def _decode(self, frame: str, source_user_id: str | None) -> tuple[SourceEvent, ...]:
        try:
            message = json.loads(frame)
        except ValueError:
            # The length, never the frame: a proxy's error page is harmless
            # but a frame this channel failed to parse is not necessarily,
            # and `Data` is the one place a token could plausibly appear.
            logger.debug("push frame was not JSON; skipped ({length} bytes)", length=len(frame))
            return ()
        if not isinstance(message, Mapping):
            return ()
        return to_source_events(message, source_user_id=source_user_id)


async def connect_websocket(
    url: str,
    *,
    open_timeout: float = 10.0,
    ping_interval: float = 20.0,
    ping_timeout: float = 20.0,
    max_queue: int = 256,
    proxy: str | Literal[True] | None = True,
) -> PushConnection:
    """The default `PushConnector`: the shared client, named by Emby's path."""
    return await _connect_websocket(
        url,
        label=WEBSOCKET_PATH,
        logger_name=_SOCKET_LOGGER_NAME,
        open_timeout=open_timeout,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        max_queue=max_queue,
        proxy=proxy,
    )
