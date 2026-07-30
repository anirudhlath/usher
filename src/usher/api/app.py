"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from usher.api.routers import health
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.telemetry import configure_telemetry


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
    app.include_router(health.router)
    return app
