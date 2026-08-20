"""The one line that lets a browser open the trace for its own request.

Every request already has a real server span — `FastAPIInstrumentor` is wired
unconditionally in `create_app` and `configure_tracing` installs a real
`TracerProvider` whether or not anything is listening on OTLP — so a valid
trace id exists for every response this process sends. Until this module it
never left the process, and `web/docs/patterns.md` §3's *"`Problem` MUST render
'Open trace' into Tempo"* was a feature that could not fire on any deployment.

**A response header rather than a member of the problem envelope**, and the
reasons are about what each of those two things is:

* The envelope is a closed contract. `ProblemResponse` has six members and a
  seven-member `code` vocabulary that ADR-0030 closes, `openapi-typescript`
  regenerates the console's types from it, and `tests/unit/test_api_problem.py`
  pins its shape. A trace id is not part of *what went wrong*; it is a fact
  about the exchange that carried the answer.
* A header is available on **success**. An operator asking why `/home` took
  four seconds wants the id exactly as much as one reading a 503, and there is
  no problem document on a 200 to put it in.
* It is readable from JavaScript with no `Access-Control-Expose-Headers`,
  **because the console is same-origin now**. That is new: `api/console.py`
  serves the bundle from this same FastAPI app, so `res.headers.get(...)`
  simply works. The previous reference client sat behind its own nginx on
  another origin, where this header would have been invisible to `fetch` and
  would have needed the expose list — which is why the absence of that list
  here is worth stating rather than assuming.

**Where it sits in the stack, which is not obvious and is measured rather than
assumed.** `FastAPIInstrumentor.instrument_app` does not call `add_middleware`;
it monkey-patches `app.build_middleware_stack` and rebuilds the whole stack as

    ServerErrorMiddleware
      └── OpenTelemetryMiddleware          ← starts the server span
            └── ServerErrorMiddleware
                  └── ExceptionHandlerMiddleware
                        └── [user middleware]   ← this
                              └── ExceptionMiddleware   ← the two handlers
                                    └── router

`add_middleware` puts this class in the `[user middleware]` slot, and two
consequences follow from that position:

* **The server span is current when this runs.** `OpenTelemetryMiddleware`
  holds the span open with `trace.use_span` around everything inside it, so
  `trace.get_current_span()` here is the *server* span rather than the internal
  `http send` span the instrumentation opens later, inside its own `send`.
  Reading it at `http.response.start` — before delegating — is what keeps that
  true.
* **`ExceptionMiddleware` is *inside* this**, so
  `validation_error_without_the_request_body`'s 422 and
  `http_error_as_a_problem_document`'s 404/405 both flow back out through this
  `send` and carry the header. A 404 problem document is exactly when somebody
  wants the link, so that is asserted rather than inferred
  (`tests/unit/test_api_trace_response.py`).

⚠️ **The one response this does not reach is the bare 500 Starlette's
`ServerErrorMiddleware` synthesises for an unhandled exception**, because that
middleware sends through the `send` it was given rather than the one it passed
down, and both of its instances sit outside this slot. Measured, not assumed.
There is no `add_middleware` position that fixes it and the alternatives are
both worse: OpenTelemetry's own `set_global_response_propagator` reaches it but
is a *process* global (the same shape as the tracer/meter providers this
project has already been bitten by), does not honour `is_recording()`, and
lives in a module its own docstring calls experimental; and overriding
`build_middleware_stack` to wrap the finished stack makes
`instrument_app`'s `isinstance(..., ServerErrorMiddleware)` check fail, at which
point it logs one line and **skips FastAPI instrumentation entirely** — trading
the header on a 500 for the span on every request.

A raw ASGI middleware rather than `BaseHTTPMiddleware`, deliberately: `GET
/events` is a long-lived `text/event-stream` and `BaseHTTPMiddleware` runs the
downstream app in its own task and wraps `receive`, which is precisely the
machinery `StreamingResponse`'s disconnect handling depends on. This touches one
message type and passes every other byte through untouched.
"""

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
