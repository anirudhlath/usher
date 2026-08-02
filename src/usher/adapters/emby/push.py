"""Emby's WebSocket push channel (PRD 03, ADR-0004).

**An open socket is not a health signal, and this module is where that is
made structural rather than remembered.** ADR-0004's live verification
recorded that a handshake against a *nonexistent path* also upgrades and
also receives `Sessions`, so Emby's listener holds a socket regardless of
path -- and a reverse proxy that forwards `Upgrade` and then buffers
produces the same state without any help from Emby. In both, the connection
object is fine and nothing is arriving. Every answer this module gives about
push health comes from `PushHealth`, which counts *messages*, and a channel
that stops delivering raises out of its own iterator rather than sitting
there looking well.

Four seams, each with a reason:

1. **`PushConnection` is an ABC**, not a structural type, even though it is
   not a port. The real `websockets.ClientConnection` cannot subclass it, so
   a wrapper has to exist -- and the wrapper is where the exception
   translation lives. That matters more here than anywhere else in this
   package: `websockets.exceptions.InvalidURI.__str__` contains the URI, and
   this channel's URI contains the session token. `EmbySession` interpolates
   `{exc}` into its own messages and explains why that is safe *there*
   (httpx exceptions carry a method and a URL, and Usher's own outbound URLs
   carry no token -- the session rides in the `X-Emby-Token` header). It is
   not safe here, and nothing but the wrapper stands between the two.
2. **`recv(timeout)` raises `TimeoutError` for "nothing arrived yet"**,
   which the caller treats as a *tick* rather than a failure. That is what
   lets the staleness watchdog run on an injected clock instead of on real
   wall time, and it is the difference between a 90-second test and a
   sub-millisecond one.
3. **`SessionLike` is a `Protocol` and `PushConnection` is an ABC**, ten
   lines apart, and the difference is the one thing ADR-0001's argument
   turns on: whether an implementation can inherit. `EmbySession` already
   has both methods and lives in this same package, so making it inherit
   would have `session.py` import `push.py` -- the wrong direction, and one
   import from a cycle. ADR-0001 governs *ports*; neither of these is one.
4. **The URL is built inside `_socket_url` and is never stored, returned,
   logged, or interpolated into an exception.** Every error message this
   module raises names a path, never a URL. ADR-0012's handling rules apply
   to it unchanged.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover -- typing only; see `connect_websocket`
    from websockets.asyncio.client import ClientConnection

from usher.adapters.emby.mapping import library_ids, user_data_states
from usher.ports.errors import PortUnavailable, UsherPortError
from usher.ports.source import SourceEvent, SourceEventKind

WEBSOCKET_PATH = "/embywebsocket"

# ADR-0004's own subscription, verbatim: the frame its end-to-end session
# sent before `Sessions` and `UserDataChanged` started arriving, and the one
# thing about this channel's protocol that was measured against the live
# server rather than read. `"0,1000"` is the listener's
# `initialDelayMs,intervalMs` pair -- **confirmed 2026-08-02**, by the one
# socket that honours it literally: an *unauthenticated* connection receives
# `Sessions` at ~1 Hz. An authenticated one does not (see
# `DEFAULT_STALE_AFTER_SECONDS`). Without this frame Emby holds the socket
# open and sends nothing -- which is indistinguishable from every other
# upgraded-but-silent failure this module exists to detect, arrived at by
# forgetting one line.
SUBSCRIBE_FRAME = '{"MessageType": "SessionsStart", "Data": "0,1000"}'

# How long one `recv` waits before reporting "nothing yet". A *tick*, not a
# timeout: each one runs the staleness watchdog. Small enough that a channel
# crossing `stale_after` is noticed within a few seconds of doing so, large
# enough that an idle lane is not spinning.
DEFAULT_POLL_SECONDS = 5.0

# How long a channel may deliver nothing at all before it is treated as
# dead. A setting (`push_stale_after_seconds`) rather than a constant,
# because the cadence it is measured against is a property of the
# deployment rather than of the protocol.
#
# **Measured 2026-08-02 against the live server, and it is not the interval
# the frame above asks for.** An authenticated socket's `Sessions` arrives
# when its row-filtered view changes, not on the 1 s timer: **median 34.7 s,
# p90 46.3 s, max 60.1 s** over 133 intervals in 70 minutes. So 90.0
# survives -- with **1.5x** headroom over the worst gap seen, and the worst
# gap grew as the window did (52.6 s at 26 minutes). One household, one
# hour, a change-driven signal; a 75-second probe earlier the same evening
# saw exactly one frame, which is what the headroom is for. A quieter server
# can exceed any fixed ceiling, and the consequence is bounded and visible
# rather than silent: the lane reconnects, the gap-closing delta returns 0
# items, and `usher.source.push.reconnects` climbs.
DEFAULT_STALE_AFTER_SECONDS = 90.0

# The `websockets` client logs its own request line at DEBUG --
# `websockets/client.py:294`, `logger.debug("> GET %s HTTP/1.1",
# request.path)` -- and this channel's request path is
# `/embywebsocket?api_key=<token>&deviceId=<id>`.
# `usher.telemetry.configure_logging` forces `propagate = True` on every
# logger that exists when it runs and installs an intercept handler on root
# at level 0, so at `log_level="DEBUG"` that line is a structured log record
# carrying the session token. Reproduced against the real library, client
# and server on `127.0.0.1`, before this existed. PRD 08: credentials are
# never logged, "including in error paths and request dumps".
_SOCKET_LOGGER_NAME = "usher.source.emby.socket"


def socket_logger() -> logging.Logger:
    """A logger `websockets` can write to and nothing can read from.

    **The level is the durable half and the other two are belt and braces.**
    `configure_logging` clears `handlers` and sets `propagate = True` on
    every logger in `loggerDict`, so both of those are undone the next time
    an app is built -- and a socket outlives the call that opened it, so
    "the next time" lands *during* the connection they were protecting.
    It never touches `level`, and `logging.basicConfig(level=0)` sets
    *root*'s level rather than this one's, so a level above `CRITICAL`
    survives: `Logger.isEnabledFor` consults `getEffectiveLevel()`, which is
    this logger's own because it is set, and a record that is not enabled is
    never formatted -- so the token is not interpolated, let alone emitted.

    Stronger than that in practice, and measured rather than assumed:
    `websockets.protocol.Protocol.__init__` computes
    `self.debug = logger.isEnabledFor(logging.DEBUG)` **once**, at
    construction, and every request-line, header and frame log in the
    library is behind `if self.debug`. So handing this logger to `connect`
    does not suppress those records, it stops them from being reached.

    Re-asserted on every call rather than once at import, for the reason
    above: `create_app`, `usher.cli.main` and dozens of tests each call
    `configure_logging`, at times import order says nothing about.

    **What this costs.** The library's own handshake, frame and close-code
    diagnostics are gone. That is a real loss when debugging a socket, and
    it is paid for by this module's own structured logging (which carries a
    `redact_query`'d URL and the ledger's counters) and by `usher push
    --probe`, which reports what actually arrived. It is not a trade against
    PRD 08's rule; that rule has one documented exception in v1
    (ADR-0012's playback URL) and this is not it.
    """
    silenced = logging.getLogger(_SOCKET_LOGGER_NAME)
    silenced.setLevel(logging.CRITICAL + 1)
    silenced.propagate = False
    if not silenced.handlers:
        silenced.addHandler(logging.NullHandler())
    return silenced


@dataclass(slots=True)
class PushHealth:
    """What is known about a push channel, from messages rather than from a
    socket.

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
        """A connection is up. Says nothing about whether it works.

        Three fields move here and they move in different directions,
        deliberately.

        `messages_received` and `reconnects` are **not** reset: they are the
        *lane's* history, and a reconnect that zeroed the count would make a
        channel that has been delivering for hours read as one that has
        never delivered -- which is a lie in the other direction, and which
        `PushSupervisor`'s "reset the failure counter only on evidence of
        delivery" rule would then act on.

        `reconnects` is incremented **here, on the second and later open**,
        rather than on a failure. That is the quantity PRD 10's dashboard
        plots: a lane that failed to connect five times and then succeeded
        reconnected *once*, and a counter on the failure would report five
        and make an unreachable source look like a flapping one. The first
        open is not a reconnect, which is why the guard is `opened_at is not
        None` rather than an unconditional `+= 1` -- otherwise every source
        starts its dashboard at 1.

        `last_message_at` **is** cleared, because it is evidence about a
        socket that is now closed. Carrying it across would let a fresh
        connection that upgrades and then buffers inherit its predecessor's
        freshness and report `is_delivering` -- the exact state this module
        exists to refuse -- and would have the watchdog measure silence from
        an instant on a connection nobody is holding. `silent_for` then
        falls back to `opened_at`, which is what makes a channel that never
        delivers anything become measurably silent.
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

        **All three clauses, and the middle one is the milestone.**
        `connected` alone is the answer this whole design refuses to give:
        it is `True` for a proxy that upgraded and buffers, for a NAT entry
        that has been dropped while both ends still believe otherwise, and
        for ADR-0004's own control handshake against a path that does not
        exist. `messages_received > 0` is what those cannot satisfy.

        The staleness clause is what keeps the answer honest *after* the
        first message: a socket that delivered once an hour ago and nothing
        since is not working, and `websockets`' `ping_interval`/
        `ping_timeout` cannot tell -- a peer answering pongs while
        delivering nothing passes the keepalive and fails this.
        """
        return (
            self.connected
            and self.messages_received > 0
            and self.last_message_at is not None
            and now - self.last_message_at <= self.stale_after
        )

    def silent_for(self, *, now: float) -> float:
        """Seconds since anything last arrived, measured from the open when
        nothing has.

        Zero before a connection exists. That branch is unreachable from the
        loop that calls this (it runs only while a connection is open), and
        it is spelled rather than left to a `None`-minus-`float` `TypeError`
        that would take a lane down instead of reconnecting it.
        """
        since = self.last_message_at if self.last_message_at is not None else self.opened_at
        return 0.0 if since is None else now - since


class PushConnection(ABC):
    """One open push connection, as this adapter needs it.

    Deliberately three methods and no state: everything about *what a
    message means* belongs to `EmbyPushChannel`, and everything about *how
    bytes move* belongs to the implementation. That split is what makes a
    loopback test a test of the real transport and the unit tests a test of
    the real logic.
    """

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send one text frame. Raises `PortUnavailable` on any failure."""

    @abstractmethod
    async def recv(self, timeout: float) -> str:
        """One text frame.

        Raises **`TimeoutError`** when nothing arrived within `timeout` --
        which the caller treats as a tick, not a failure, and uses to run
        its staleness watchdog. Raises `PortUnavailable` when the connection
        is gone, and **never returns to signal that**: a `recv` that ended
        quietly would be indistinguishable from a source with nothing to
        say, which is the same failure `SourceAdapter.list_items` forbids
        one layer up.

        Binary frames are decoded as UTF-8 with `errors="replace"` rather
        than raising. Emby sends text; a frame that is neither is counted as
        a received message (it is evidence the socket is alive) and then
        fails to parse as JSON, which the caller already handles.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release the connection. Idempotent, and never raises."""


_clock = time.monotonic


# Emby's `LibraryChanged` arrays, and the event each becomes. `ItemsRemoved`
# is mapped and then deliberately does nothing downstream: ADR-0015 says
# availability is retracted only by a walk that provably finished, and an
# Emby library refresh emits `ItemsRemoved` for items that have not gone
# anywhere. `PushApplyService` counts it and leaves the row available for the
# nightly sweep -- PRD 08 prices that as "availability goes stale, not
# wrong". The mapping exists so the *event* is expressible and countable
# rather than being invisible in the message.
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

    **A message that maps to nothing is still a message.** `Sessions` -- the
    periodic one ADR-0004 observed -- produces no event by design, because
    deriving anything from it would mean tracking play sessions Usher never
    starts. Its value is that it arrives, and the counting happens in
    `EmbyPushChannel` on every frame, before this function is consulted. An
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
    session, an httpx client and a credential -- and so the dependency is
    *two methods* rather than "an `EmbySession`", which is what keeps a
    later reader from reaching for `request()` from inside a socket loop.

    **A `Protocol`, and `PushConnection` twelve lines up is an `ABC`.** That
    is not an inconsistency and it is not ADR-0001 being ignored: ADR-0001
    governs *ports*, and neither of these is one. The two seams differ in
    the thing ADR-0001's argument turns on -- whether an implementation can
    inherit. `websockets.ClientConnection` cannot, so `PushConnection` needs
    a wrapper anyway and gets fail-fast instantiation for free. `EmbySession`
    already has both methods, in the same package, and making it inherit
    from here would have `session.py` import `push.py` -- the wrong
    direction, and one import away from a cycle the day this module wants a
    session. The plan specified an ABC for both; that version does not
    type-check at the call site, because `EmbySession` is not a subclass and
    `abc.register()` is invisible to mypy.
    """

    async def access_token(self) -> str: ...

    async def user_id(self) -> str: ...


class EmbyPushChannel:
    """One `/embywebsocket` connection, and the ledger that says whether it
    is working.

    Reuses `EmbySession` rather than authenticating: PRD 03's durable-client
    property comes from authenticating *once* with a stable `DeviceId`, and
    verified 2026-07-31, presenting an existing token alongside a different
    `DeviceId` neither forks nor invalidates the session -- Emby binds a
    session to the token's own authentication record. A channel that
    authenticated per reconnect would mint a session per reconnect and undo
    the one property the header exists for. Reusing the session also
    inherits its single-flight re-authentication, its negative cache, and
    its exactly-one-retry for free.
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
        """`/embywebsocket?api_key=<token>&deviceId=<id>`, built and handed
        straight to the connector.

        **Never stored on the instance, never returned to a caller outside
        this module, never logged, never interpolated into an exception,
        never a span attribute.** ADR-0012's handling rules, applied to the
        second place this token is materialised. `quote` on both values
        because an `external_id`-shaped rule applies to a device id too: it
        is a persisted string an operator could have influenced, and
        `EmbyAdapter._segment` documents the same reasoning for a path.

        The token is read from the session on **every** open rather than
        cached here, so a channel that reconnects after a silent
        re-authentication presents the new token; a cached one would present
        a revoked credential forever.

        `http`/`https` become `ws`/`wss`. Emby accepts either scheme on this
        route, and using the WebSocket scheme is what keeps a reader from
        wondering; `websockets` requires one.
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
            # A bare `except Exception` on purpose, and carrying no
            # suppression directive: the plan wrote one for `BLE001`, which
            # is not in this project's ruff selection, and `RUF100` -- which
            # is -- rejects a directive for a rule nothing enables. The
            # connector is arbitrary third-party code and *anything* it
            # raises must become this port's vocabulary rather than
            # escaping to `PushSupervisor` untranslated.
            #
            # `type(exc).__name__`, never `{exc}`. The connector's own
            # exceptions can carry the URI (`websockets.exceptions.InvalidURI`
            # does), and that URI carries the session token. `EmbySession`
            # interpolates `{exc}` and explains why that is safe there;
            # nothing about that argument transfers to this URL.
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
            # One cooperative yield per iteration, and it is not decoration.
            # This is a `while True` whose only other await is `recv`, and
            # `recv` is permitted to complete *without suspending* --
            # `websockets`' does exactly that whenever frames are already
            # buffered, which on a busy socket is most of the time. PRD 01's
            # concurrency model is one process with per-lane semaphores, so
            # this lane shares an event loop with the HTTP server and the
            # job worker; a lane that can run unbounded iterations without
            # yielding starves both.
            #
            # **This line is a known mutation survivor and is kept anyway**,
            # for the reason `jobs.py` keeps its `GREATEST` alongside its
            # `WHERE`: no test can kill it, because the only observer of a
            # starved event loop would itself be on that loop. Deleting it
            # passes all 55 cases here, since `FakePushConnection.recv` does
            # suspend. What it is worth was measured the other way round:
            # with the fake's suspension removed *and* this line absent, the
            # suite does not fail, it **hangs** -- 37 cases in, then nothing,
            # killed at 90 s, because `asyncio.wait_for` needs the loop to
            # run in order to fire. With this line present that same
            # mutation fails one case in 0.6 s.
            #
            # Re-measured at 55 cases in M5 group C rather than renumbered
            # from the 38 this said when it was written: a count inside a
            # mutation result is part of the measurement, and the suite it
            # counts had grown by 17 cases since.
            await asyncio.sleep(0)
            try:
                frame = await connection.recv(self._poll_seconds)
            except TimeoutError:
                # A tick, not a failure -- and the tick is what runs the
                # watchdog. `PushHealth.is_delivering` makes an
                # upgraded-but-silent socket *report* unhealthy; this is
                # what makes the lane stop using one.
                #
                # Deliberately not what `websockets`' own
                # `ping_interval`/`ping_timeout` covers. Those detect a dead
                # TCP peer. A *live* peer that answers pongs and delivers
                # nothing passes them and fails this -- and that peer is
                # exactly what ADR-0004 measured, where a handshake against
                # a nonexistent path upgraded and was held open. The two are
                # layered; neither substitutes for the other.
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
        """Raise `PortUnavailable` when nothing has arrived for
        `stale_after`.

        **Raises rather than reconnecting**, and that is the whole reason
        this is one line rather than a loop. A channel is one connection;
        reconnect belongs to `PushSupervisor`, because PRD 03 puts the
        gap-closing delta reconcile *on* the reconnect. A channel that
        quietly re-established its own socket would skip that walk and leave
        the supervisor's consecutive-failure counter with nothing to count,
        so a permanently broken proxy would look like a permanently healthy
        lane -- which is the same lie `is_delivering` refuses, arrived at
        from the other side.

        Silence is measured from the last message, falling back to the open
        (`PushHealth.silent_for`), so a channel that has *never* delivered
        becomes stale too. That fallback is the case this milestone is named
        for: without it the one failure mode the watchdog exists to catch is
        the one it cannot see.

        **The message names a duration and a path, never a URL.** This
        string reaches a log line and `SourceStatus.detail`, and the URL it
        would otherwise name carries the session token (ADR-0012).
        """
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


class _WebsocketsConnection(PushConnection):
    """`websockets.asyncio.client.ClientConnection`, behind this adapter's
    own three methods.

    The wrapper exists so that **no `websockets` exception ever crosses into
    `usher.ports.errors` carrying its own message.**
    `websockets.exceptions.InvalidURI.__str__` is
    `f"{self.uri} isn't a valid URI: {self.msg}"` -- read from the installed
    library, not assumed -- and this channel's URI carries the session
    token; `InvalidProxy` has the same shape for a proxy URL, and
    `InvalidStatus` carries the response. `EmbySession` interpolates `{exc}`
    into its own messages and explains why that is safe *there* (httpx
    exceptions carry a method and a URL, and Usher's own outbound URLs carry
    no token -- the session rides in the `X-Emby-Token` header). Nothing
    about that argument transfers here, and the difference is the kind of
    thing a later reader would "unify". Every translation below names the
    exception's *type* and nothing else.
    """

    def __init__(self, connection: "ClientConnection") -> None:
        self._connection = connection

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except Exception as exc:
            # A bare `except Exception` and no suppression directive, for
            # the reason `EmbyPushChannel.open` records: `BLE001` is not in
            # this project's ruff selection and `RUF100` -- which is --
            # rejects a directive for a rule nothing enables.
            raise PortUnavailable(f"{WEBSOCKET_PATH} send failed: {type(exc).__name__}") from exc

    async def recv(self, timeout: float) -> str:
        """One text frame, or `TimeoutError` when nothing arrived in time.

        `asyncio.wait_for` around the library's own `recv()` rather than a
        library-level deadline, because there is no library-level deadline:
        `websockets` documents cancelling `recv` as safe ("there's no risk
        of losing data; the next invocation will return the next message")
        and names `asyncio.wait_for` as the way to enforce a timeout.
        """
        try:
            frame = await asyncio.wait_for(self._connection.recv(), timeout)
        except TimeoutError:
            # Re-raised as itself: the caller's watchdog owns this, and
            # translating it to `PortUnavailable` here would make every idle
            # poll a reconnect and a gap-closing delta walk.
            raise
        except Exception as exc:
            raise PortUnavailable(f"{WEBSOCKET_PATH} closed: {type(exc).__name__}") from exc
        # Emby sends text. A binary frame is decoded rather than raising: it
        # is still evidence the socket is alive, and it then fails to parse
        # as JSON, which the channel already drops-and-counts.
        return frame if isinstance(frame, str) else bytes(frame).decode("utf-8", "replace")

    async def aclose(self) -> None:
        try:
            await self._connection.close()
        except Exception as exc:
            # Never raises: this runs in a `finally` that is itself often
            # unwinding the `PortUnavailable` explaining why the lane
            # dropped, and a close failure replacing the real reason is
            # worse than a log line. The port documents `aclose` as never
            # raising for exactly this.
            logger.debug("push connection close failed: {kind}", kind=type(exc).__name__)


async def connect_websocket(
    url: str,
    *,
    open_timeout: float = 10.0,
    ping_interval: float = 20.0,
    ping_timeout: float = 20.0,
    max_queue: int = 256,
    proxy: str | Literal[True] | None = True,
) -> PushConnection:
    """The default `PushConnector`: a real `websockets` client, wrapped.

    **`ping_interval=20` is PRD 03's heartbeat and is the library's own
    default.** "Emby sends no keepalive of its own. nginx closes idle
    connections at 60 s and Cloudflare at ~100 s, so the client must
    generate traffic." A WebSocket ping frame is traffic. It is passed
    explicitly rather than left to the default so that a future default
    change is a diff rather than a silent regression. It is **layered with**
    the staleness watchdog rather than an alternative to it: this detects a
    dead TCP peer, and the watchdog detects a live peer that has stopped
    delivering.

    **`max_queue=256`, not the default 16.** The supervisor runs a
    gap-closing delta reconcile *after* connecting, with the socket already
    live, precisely so nothing that happens during the walk is missed --
    and at the default the client stops reading after 16 buffered frames and
    applies TCP backpressure to the server for the length of that walk. 256
    frames of `UserDataChanged` is a few hundred kilobytes; the walk it
    covers is minutes.

    **`logger=socket_logger()` is the only argument here that is a security
    control rather than a tuning knob.** See `socket_logger`.

    **`proxy` is passed through with the library's own default, `True`,
    which means "resolve one from the environment".** A household fronting
    its Emby with a reverse proxy is a real deployment and
    `HTTPS_PROXY`/`WS_PROXY` is how an operator says so, so the default
    stays. It is a parameter rather than a constant because
    `websockets.proxy.get_proxy` consults `urllib.request.proxy_bypass`,
    which does **not** exempt loopback unless `no_proxy` names it -- so a
    developer machine with `HTTP_PROXY` set would send a `127.0.0.1`
    connection through it, and `tests/integration/test_push_loopback.py`
    passes `proxy=None` for exactly that reason. Never logged and never
    interpolated into an exception: `websockets.exceptions.InvalidProxy`
    carries the proxy URL, which is a credential in the same way this
    channel's own URL is.

    The import is **local**, not module-scope: `usher.adapters.emby` is
    imported by the factory on every composition-root build, and
    `websockets` is a dependency only the push lane needs.
    """
    from websockets.asyncio.client import connect

    connection = await connect(
        url,
        open_timeout=open_timeout,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        max_queue=max_queue,
        logger=socket_logger(),
        proxy=proxy,
    )
    return _WebsocketsConnection(connection)
