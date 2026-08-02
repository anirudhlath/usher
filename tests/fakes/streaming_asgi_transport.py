"""An httpx transport that streams an ASGI response instead of buffering it.

**`httpx.ASGITransport` cannot test SSE, and the failure mode is a hang
rather than an error.** Read its `handle_async_request`: it runs
`await self.app(scope, receive, send)` *to completion*, collects every
`http.response.body` message into a list, and only then builds a `Response`
whose stream yields `b"".join(body_parts)`. `GET /events` never completes --
that is what a stream is -- so `client.stream("GET", "/events")` against it
blocks forever inside the transport and no case in this file would ever run.
The M5 plan's own draft of `tests/unit/test_api_events.py` was written
against it; this module is what makes those cases runnable.

So: the app runs in a task, `http.response.start` resolves a future the
request waits on, and each `http.response.body` goes onto a queue the
response's stream drains. Closing the response sends `http.disconnect`,
which is what Starlette's `StreamingResponse` cancels its body iterator on --
so the route's `finally` really runs and the bus really loses its subscriber.

**Where this is more forgiving than a real server**, in the shape every fake
in this repository owes:

- **No HTTP at all.** No chunked framing, no `Content-Length` negotiation, no
  header validation. `Connection: keep-alive` is a hop-by-hop header a real
  server may rewrite or reject, and nothing here would notice.
- **No socket, so no backpressure.** The chunk queue is unbounded, where a
  real client that stops reading eventually fills a kernel buffer and stalls
  `send`. A route that produced faster than a browser consumed looks fine
  here.
- **No proxy.** `X-Accel-Buffering: no` is asserted as a *string* because
  only a real nginx can act on it, which is the whole reason that header is
  asserted rather than trusted.
- **`aclose()` disconnects politely.** A real client vanishing gives the
  server an abrupt reset, and the difference is visible to anything that
  distinguishes them.

`tests/integration/test_sse_end_to_end.py` drives a real app through a real
request and is what closes the first two; only a deployment behind a real
nginx closes the third.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from types import TracebackType
from typing import Any

import httpx

_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[MutableMapping[str, Any], _Receive, _Send], Awaitable[None]]

# How long `aclose()` waits for the app task to notice `http.disconnect` and
# unwind. Bounded rather than awaited forever: a route whose cleanup blocks
# is a defect this transport must report as a failed test rather than as a
# hung suite.
_SHUTDOWN_SECONDS = 5.0


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: asyncio.Queue[bytes | None], transport: "_Call") -> None:
        self._chunks = chunks
        self._call = transport

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._chunks.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        await self._call.disconnect()


class _Call:
    """One in-flight ASGI call, and the disconnect that ends it."""

    def __init__(self) -> None:
        self.disconnected = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def disconnect(self) -> None:
        self.disconnected.set()
        if self.task is None or self.task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=_SHUTDOWN_SECONDS)
        except TimeoutError:  # pragma: no cover - a route whose cleanup blocks
            self.task.cancel()
            raise


class StreamingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: _ASGIApp, *, root_path: str = "") -> None:
        self._app = app
        self._root_path = root_path

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        call = _Call()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        started: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = (
            asyncio.get_running_loop().create_future()
        )
        request_sent = False

        async def receive() -> _Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await call.disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: _Message) -> None:
            if message["type"] == "http.response.start":
                if not started.done():
                    started.set_result((message["status"], list(message.get("headers", []))))
            elif message["type"] == "http.response.body":
                piece = message.get("body", b"")
                if piece:
                    await chunks.put(piece)
                if not message.get("more_body", False):
                    await chunks.put(None)

        async def run() -> None:
            try:
                await self._app(self._scope(request), receive, send)
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)
                raise
            finally:
                # A terminal chunk however the app ended, so a reader waiting
                # on the queue sees the end rather than the hang an app that
                # raised before its last `more_body=False` would otherwise
                # produce.
                await chunks.put(None)

        call.task = asyncio.create_task(run())
        try:
            status, headers = await started
        except BaseException:
            await call.disconnect()
            raise
        return httpx.Response(status, headers=headers, stream=_ChunkStream(chunks, call))

    def _scope(self, request: httpx.Request) -> MutableMapping[str, Any]:
        return {
            "type": "http",
            # `spec_version` matches uvicorn 0.51's own (`2.3`) rather than
            # being omitted. It is load-bearing: `StreamingResponse.__call__`
            # takes the task-group-plus-`listen_for_disconnect` path below
            # 2.4 and a bare `stream_response` at or above it, and only the
            # first cancels the body iterator when a client goes away --
            # which is the whole of `test_a_disconnect_unsubscribes`.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": self._root_path,
        }

    async def __aenter__(self) -> "StreamingASGITransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        return None
