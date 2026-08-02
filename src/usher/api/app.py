"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from usher.api.errors import validation_error_without_the_request_body
from usher.api.routers import events, health, sources, titles
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.services.events import InMemoryEventBus
from usher.telemetry import configure_telemetry, register_sse_gauge


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings.database_url.get_secret_value())
        app.state.session_factory = build_session_factory(engine)
        try:
            yield
        finally:
            # Not just hygiene: verified directly that a bare `yield` with
            # no try/finally skips this call entirely if the task running
            # the lifespan is cancelled while suspended at yield (as
            # opposed to __aexit__ being called normally) -- exactly the
            # shape ASGI shutdown uses. Harmless today (M1 has nothing
            # else in the lifespan to leak), but M5 onward adds websocket
            # connections, job workers, and HTTP clients to this same
            # lifespan, where a skipped cleanup call is a real leak, not a
            # theoretical one.
            await engine.dispose()

    app = FastAPI(
        title="Usher",
        version="0.1.0",
        description="A self-hosted media catalog backend.",
        lifespan=lifespan,
    )
    # Gives every request a real server span (a valid trace/span id, even
    # with no OTLP collector configured -- see configure_tracing) so
    # inject_trace_context has something to correlate logs against, and so
    # later milestones' explicit pipeline spans nest under a request trace
    # instead of each becoming its own root. Per-app-instance and safe to
    # call on every create_app(): instrument_app marks the app object
    # itself, not a process-global singleton (verified directly).
    FastAPIInstrumentor.instrument_app(app)
    # The configuration handlers read, via `deps.get_app_settings`. Set here
    # rather than in the lifespan because it is not a resource with a
    # lifetime -- and because `create_app(settings)`'s whole point is that
    # the app runs on the settings it was handed, not on whatever the
    # environment says at the moment a request arrives.
    app.state.settings = settings
    # The process-wide client event bus (PRD 07's SSE channel). Here rather
    # than in the lifespan for the reason `settings` is: it is not a resource
    # with a lifetime -- no connection, no thread, nothing to dispose -- and
    # `get_reconcile_service` needs one on every request that walks a source.
    # One per app, never per request: a request-scoped bus would give every
    # SSE connection its own and a publisher would fan out to nobody.
    bus = InMemoryEventBus(buffer_size=settings.sse_buffer_size, queue_size=settings.sse_queue_size)
    app.state.events = bus
    # PRD 10's `usher.sse.connections`, and the one observable callback in
    # this project that is a live read rather than a snapshot -- `len()` on an
    # in-memory set has no coroutine to bounce onto the event loop from the
    # metric reader's background thread. Re-registering on a second
    # `create_app()` in one process is deliberate and is why the reader is a
    # module global rather than a captured closure.
    register_sse_gauge(lambda: bus.subscribers)
    # Replaces FastAPI's default 422 body, which echoes the submitted
    # request -- and `POST /admin/sources` submits a source credential. See
    # usher.api.errors; this is a security control, not a response-shape
    # preference, and it is registered here so it covers every route rather
    # than only the one that made it necessary.
    app.add_exception_handler(RequestValidationError, validation_error_without_the_request_body)
    app.include_router(events.router)
    app.include_router(health.router)
    app.include_router(sources.router)
    app.include_router(titles.router)
    return app
