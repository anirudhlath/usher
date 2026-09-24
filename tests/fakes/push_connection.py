"""A push connection that really awaits, and a connector that hands them out."""

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
        # none of them, so recording them is the only thing that can observe
        # a caller passing the wrong one.
        self.recv_timeouts: list[float] = []
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        self._failure: PortUnavailable | None = None
        self._stalled = False

    # -- what a test arranges -------------------------------------------

    def deliver(self, frame: str) -> None:
        """Queue a frame.

        Synchronous, so a test can arrange before it awaits.
        """
        self._frames.put_nowait(frame)

    def stall(self) -> None:
        """Deliver nothing from here on, whatever is already queued.

        `recv` then times out on every call, which is the *only* thing an
        upgraded-but-silent socket does differently from a healthy one.
        """
        self._stalled = True

    def drop(self, message: str = "connection closed by peer") -> None:
        """Fail every later `recv` with the `PortUnavailable` a closed socket becomes."""
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
