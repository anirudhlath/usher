# tests/unit/test_fakes_slow_transport.py
"""`SlowTransport`'s own fidelity, where it stands in for `MockTransport`.

Two suites drive `FakeEmbyServer` through *both* transports, so anywhere
this one behaves differently is a place a test passes or fails for a reason
that has nothing to do with the adapter. Its `max_in_flight` counter is
worse than that: the session and adapter single-flight tests assert on it
to prove they are genuinely concurrent, so an instrument that can over-read
is one that can certify a test as meaningful when it is not.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.fakes.slow_transport import SlowTransport


async def test_a_streaming_request_body_is_read_before_the_handler_sees_it() -> None:
    """`httpx.MockTransport` awaits `request.aread()` before calling its
    handler; this one did not. `FakeEmbyServer._authenticate` reads
    `request.content`, which raises `httpx.RequestNotRead` for a request
    whose body has not been read -- latent only because `EmbySession`
    happens to build every request with `json=`, which is already bytes.
    """
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={})

    async def body() -> AsyncIterator[bytes]:
        yield b'{"Username": "usher"}'

    transport = SlowTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://emby.invalid") as client:
        await client.post("/Users/AuthenticateByName", content=body())
    assert seen == [b'{"Username": "usher"}']


async def test_a_cancelled_request_does_not_leak_the_in_flight_counter() -> None:
    """The counter is the instrument the single-flight tests read to prove
    they forced real concurrency, and it could only ever lie *upward*: the
    increment sat before the `try` and the `await asyncio.sleep` sat outside
    it, so a cancellation during the sleep skipped the `finally` and left
    `in_flight` permanently high. Every later request then read a
    `max_in_flight` inflated by a request that was not actually in flight --
    the instrument failing in exactly the direction that certifies a test as
    concurrent when it has stopped being so.
    """
    transport = SlowTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport, base_url="https://emby.invalid") as client:
        task = asyncio.create_task(client.get("/System/Info/Public"))
        for _ in range(200):
            if transport.in_flight:
                break
            await asyncio.sleep(0.001)
        # Without this the cancellation below could land before the request
        # ever reached the transport, and the assertion would hold vacuously.
        assert transport.in_flight == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert transport.in_flight == 0
    assert transport.max_in_flight == 1
