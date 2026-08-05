"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from usher.api.errors import validation_error_without_the_request_body
from usher.api.lanes import LaneSupervisor
from usher.api.routers import events, health, sources, titles
from usher.composition import (
    DefaultUserId,
    embedder,
    metadata_provider,
    nothing,
    unit_of_work,
)
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.services.events import InMemoryEventBus
from usher.services.rows.cache import RowCache
from usher.telemetry import configure_telemetry, register_push_gauges, register_sse_gauge


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings.database_url.get_secret_value())
        session_factory = build_session_factory(engine)
        app.state.session_factory = session_factory
        # The TMDb provider, and the one place its token bucket can live:
        # `api/deps.py` says why it cannot be request-scoped ("N in-flight
        # requests get N x 30 rps"), and the worker lane is the only thing
        # in this process that needs it. Not built at all when no worker
        # runs here -- an idle `httpx.AsyncClient` in a push-only
        # deployment is a resource with no reader.
        provider, close_provider = (
            await metadata_provider(settings) if settings.worker_enabled else (None, nothing)
        )
        # The embedding model, on the same terms and for the same reason: one
        # per process, built only where a worker will use it. Not built at all
        # in a push-only deployment -- a 65 MB ONNX session with no reader.
        model, close_model = (
            await embedder(settings) if settings.worker_enabled else (None, nothing)
        )
        lanes = LaneSupervisor(
            settings,
            unit_of_work(session_factory, settings, events=bus, provider=provider),
            bus,
            user_id=DefaultUserId(session_factory),
            provider=provider,
            embedder=model,
            rows=row_cache,
        )
        app.state.lanes = lanes
        # PRD 10's `usher.source.push.connected` / `.reconnects`. Registered
        # unconditionally, because the reader answering "no lane, no
        # observation" is what keeps a push-disabled process from reporting
        # a fabricated zero on a series whose alert fires on exactly that.
        register_push_gauges(lanes.push_snapshots)
        # Creates tasks and opens no connection -- see `LaneSupervisor.start`.
        # That is what keeps `/health` answering 200 with Postgres down.
        await lanes.start()
        try:
            yield
        finally:
            # Not just hygiene: verified directly that a bare `yield` with
            # no try/finally skips this call entirely if the task running
            # the lifespan is cancelled while suspended at yield (as
            # opposed to __aexit__ being called normally) -- exactly the
            # shape ASGI shutdown uses. The M1 comment here said "M5 onward
            # adds websocket connections, job workers, and HTTP clients to
            # this same lifespan, where a skipped cleanup call is a real
            # leak, not a theoretical one". This is that milestone; the
            # comment stops being a prediction.
            #
            # `stop()` first, then the client, then the engine: an engine
            # disposed under a live lane makes that lane's next statement
            # raise into a task that is about to be cancelled anyway.
            await lanes.stop()
            await close_provider()
            await close_model()
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
    # The process's row and screen caches (PRD 06). Here rather than in the
    # lifespan for the reason `bus` is: it is not a resource with a lifetime --
    # a dict, no connection, nothing to dispose. **One per app, never per
    # request**: a request-scoped cache caches nothing, exactly as a
    # request-scoped bus fans out to nobody. The push lane invalidates through
    # this same object, which is why it is built before `lanes` reads it.
    row_cache = RowCache(clock=lambda: datetime.now(UTC))
    app.state.row_cache = row_cache
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
