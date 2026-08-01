"""A push connection that really awaits, and a connector that hands them out.

**Where this is more forgiving than the real thing, stated because M3's live
run found 40 contract assertions passing against a write-back that had never
once worked.** This fake performs *no handshake at all*: a wrong path, a
missing `Upgrade` header, a proxy answering 404, a TLS failure and a
subprotocol mismatch are every one of them invisible here. It never
fragments a message, never sends a binary frame, never applies backpressure
at `max_queue`, and its `aclose()` cannot fail. It has no notion of a close
code, so "the peer went away cleanly" and "the peer went away abruptly" are
the same event. And its `recv(timeout)` **ignores `timeout`'s effect
entirely**: it raises `TimeoutError` the instant a test asks it to, where
the real one raises only after that many seconds of real wall time. That
last one is the load-bearing forgiveness -- it is what lets a staleness
watchdog be tested on an injected clock in under a millisecond rather than
in a minute and a half -- and it is why the *value* is recorded even though
it is not honoured. Without `recv_timeouts` nothing here could catch a
channel that passed the wrong timeout, or none at all, and the whole
watchdog rests on `recv` returning control on a cadence the channel chose;
`test_the_channel_polls_with_its_own_timeout` is the case that reads it.
The real connection hands the same value to `asyncio.wait_for`, so a wrong
one there is a lane that either spins or never runs its watchdog.

What closes those gaps is a loopback test driving the real `websockets`
client against a real `websockets` server on `127.0.0.1` -- a real
handshake, a real ping/pong keepalive, a real abrupt close. The residual gap
after that is Emby's own listener, and only M5's live verification closes
it.

**It does really await**, which is not decoration. A bare mock never
suspends, so the event loop runs each task through its whole cycle before
starting the next -- the same reason `tests/fakes/slow_transport.py` exists,
where a deleted single-flight lock passed five runs in a row. `recv` awaits
`asyncio.sleep(0)` before it inspects anything, so a consumer parked in the
channel's own loop genuinely yields to a producer between frames, and a test
can measure that the two overlapped rather than ran one after the other.
"""

import asyncio
from collections.abc import Sequence

from usher.adapters.emby.push import PushConnection
from usher.ports.errors import PortUnavailable


class FakePushConnection(PushConnection):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.recv_calls = 0
        # The timeouts this connection was *asked* for, in order. It honours
        # none of them -- see the module docstring -- so recording them is
        # the only thing that can observe a caller passing the wrong one.
        self.recv_timeouts: list[float] = []
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        self._failure: PortUnavailable | None = None
        self._stalled = False

    # -- what a test arranges -------------------------------------------

    def deliver(self, frame: str) -> None:
        """Queue a frame. Synchronous, so a test can arrange before it
        awaits."""
        self._frames.put_nowait(frame)

    def stall(self) -> None:
        """Deliver nothing from here on, whatever is already queued.

        `recv` then times out on every call, which is the *only* thing an
        upgraded-but-silent socket does differently from a healthy one -- and
        is exactly the state ADR-0004's control handshake against a
        nonexistent path produced.
        """
        self._stalled = True

    def drop(self, message: str = "connection closed by peer") -> None:
        """Fail every later `recv` with a `PortUnavailable`, as a real
        `ConnectionClosedError` translates to."""
        self._failure = PortUnavailable(message)

    # -- PushConnection --------------------------------------------------

    async def send(self, message: str) -> None:
        if self._failure is not None:
            raise self._failure
        self.sent.append(message)

    async def recv(self, timeout: float) -> str:
        self.recv_calls += 1
        self.recv_timeouts.append(timeout)
        # A real suspension point on every call, before anything is
        # inspected: without it a consumer task runs to completion before a
        # producer task ever starts, and a test that looked concurrent is
        # not.
        await asyncio.sleep(0)
        if self._failure is not None:
            raise self._failure
        if self._stalled or self._frames.empty():
            raise TimeoutError
        return self._frames.get_nowait()

    async def aclose(self) -> None:
        self.closed = True


class FakePushConnector:
    """Hands out connections, in order, and records how many were asked for.

    A list rather than one connection, because reconnect is the thing under
    test in `services/push.py`: a connector that returned the same object
    forever could not express "the second connection is the one that
    delivers".
    """

    def __init__(self, connections: Sequence[FakePushConnection] | None = None) -> None:
        self._queued = list(connections or [])
        self.handed_out: list[FakePushConnection] = []
        self.failures: list[BaseException] = []
        self.attempts = 0

    def fail_next(self, failure: BaseException | str = "connection refused") -> None:
        """Queue one failure for the next call.

        A `str` becomes a `PortUnavailable`, which is what a real connector
        raises once it has translated. An arbitrary exception may be passed
        instead, which is how a test reaches the *untranslated* arm -- the
        one that must not interpolate the exception, because
        `websockets.exceptions.InvalidURI.__str__` contains the URI and this
        URI contains the token.
        """
        self.failures.append(PortUnavailable(failure) if isinstance(failure, str) else failure)

    async def __call__(self, url: str) -> PushConnection:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        connection = self._queued.pop(0) if self._queued else FakePushConnection()
        self.handed_out.append(connection)
        return connection
