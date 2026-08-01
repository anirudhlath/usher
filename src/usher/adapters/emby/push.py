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

Three seams, each with a reason:

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
3. **The URL is built inside `_socket_url` and is never stored, returned,
   logged, or interpolated into an exception.** Every error message this
   module raises names a path, never a URL. ADR-0012's handling rules apply
   to it unchanged.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


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

        Two fields move in opposite directions here and both are deliberate.

        `messages_received` and `reconnects` are **not** reset: they are the
        *lane's* history, and a reconnect that zeroed the count would make a
        channel that has been delivering for hours read as one that has
        never delivered -- which is a lie in the other direction, and which
        `PushSupervisor`'s "reset the failure counter only on evidence of
        delivery" rule would then act on.

        `last_message_at` **is** cleared, because it is evidence about a
        socket that is now closed. Carrying it across would let a fresh
        connection that upgrades and then buffers inherit its predecessor's
        freshness and report `is_delivering` -- the exact state this module
        exists to refuse -- and would have the watchdog measure silence from
        an instant on a connection nobody is holding. `silent_for` then
        falls back to `opened_at`, which is what makes a channel that never
        delivers anything become measurably silent.
        """
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

    def record_reconnect(self) -> None:
        self.reconnects += 1

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
