"""An httpx transport that really awaits, and counts its own overlap.

Lives here rather than beside one test because two suites need it: the
session's single-flight test (`tests/unit/test_adapters_emby_session.py`)
and the adapter's (`tests/unit/test_adapters_emby_adapter.py`), which
exercises a different code path -- `EmbyAdapter._fetch` takes the session
lock twice per call, once in `user_id()` and once in `request()`, with a
window in between that a single-lock test cannot reach.
"""

import asyncio
from collections.abc import Callable

import httpx


class SlowTransport(httpx.AsyncBaseTransport):
    """Wraps a synchronous handler with a real `asyncio.sleep`, so that N
    tasks fired via `asyncio.gather` are provably all in-flight at once
    (see `max_in_flight` below) rather than racing to completion one at a
    time -- which a bare `httpx.MockTransport` is fast enough to do, since
    it never actually awaits anything on the way to calling the handler.

    This matters because a single-flight test built on a plain
    `MockTransport` does *not* reliably prove the lock does anything:
    verified directly while writing the session's version of this test that
    deleting `EmbySession._refresh`'s `async with self._lock` entirely does
    not make it fail, on this event loop, every time tried. The likely
    reason (confirmed by instrumenting a throwaway script the same way
    `max_in_flight` does here): with no real await between "read the stale
    token" and "send the request", the event loop tends to race a single
    gathered task all the way through its own request -> 401 -> refresh ->
    retry -> success before starting the next one, so most of the gathered
    calls observe an *already refreshed* token from `_session()` and never
    race into `_refresh` at all -- the mutation is never exercised,
    regardless of whether the lock exists. A real (if tiny) delay forces
    genuine overlap.

    Assert on `max_in_flight` as well as on the behaviour under test, or
    the test can silently stop being concurrent and nobody finds out.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.in_flight = 0
        self.max_in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.02)
        try:
            return self._handler(request)
        finally:
            self.in_flight -= 1
