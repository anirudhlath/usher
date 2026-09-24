"""The one line that lets a browser open the trace for its own request."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from usher.telemetry import TRACERESPONSE_HEADER, traceresponse

#: ASGI carries header names as lowercase bytes. Encoded once, at import.
_HEADER_NAME = TRACERESPONSE_HEADER.encode("ascii")


class TraceResponseMiddleware:
    """Adds `traceresponse` to every HTTP response that has a live span."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # `lifespan` and `websocket` have no response to annotate, and a
            # `lifespan` scope has no span either.
            await self.app(scope, receive, send)
            return

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                value = traceresponse()
                if value is not None:
                    # A new list rather than an in-place `append`: the message
                    # a response object hands over may share its header list
                    # with a `Response` instance that outlives the send (a
                    # `FileResponse` is re-sent per range request), and
                    # appending to that would accumulate one header per send.
                    message["headers"] = [
                        *message.get("headers", []),
                        (_HEADER_NAME, value.encode("ascii")),
                    ]
            await send(message)

        await self.app(scope, receive, send_with_trace)
