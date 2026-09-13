"""An httpx transport that really awaits, and counts its own overlap."""

import asyncio
from collections.abc import Callable

import httpx


class SlowTransport(httpx.AsyncBaseTransport):
    """Wraps a synchronous handler with a real `asyncio.sleep`.

    so that N tasks fired via `asyncio.gather` are provably all in-flight at once (see
    `max_in_flight` below) rather than racing to completion one at a time -- which a
    bare `httpx.MockTransport` is fast enough to do, since it never actually awaits
    anything on the way to calling.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.in_flight = 0
        self.max_in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Before the counter, so a cancellation here is not one to unwind.
        await request.aread()
        self.in_flight += 1
        try:
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.02)
            return self._handler(request)
        finally:
            self.in_flight -= 1
