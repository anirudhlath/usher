"""Application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.exceptions import HTTPException as StarletteHTTPException

from usher.api.console import mount_console
from usher.api.errors import (
    http_error_as_a_problem_document,
    port_error_as_a_problem_document,
    problem_responses_carry_their_media_type,
    validation_error_without_the_request_body,
)
from usher.api.lanes import LaneSupervisor
from usher.api.routers import (
    bootstrap,
    browse,
    collections,
    events,
    health,
    home,
    images,
    meta,
    people,
    playback,
    rows,
    search,
    series,
    sources,
    titles,
    unmatched,
    watch,
)
from usher.api.trace_response import TraceResponseMiddleware
from usher.composition import (
    DefaultUserId,
    embedder,
    image_proxy,
    llm_client,
    metadata_provider,
    nothing,
    search_query_buffer,
    source_gates,
    unit_of_work,
)
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.ports.errors import PortAuthFailed, PortRateLimited
from usher.services.events import InMemoryEventBus
from usher.services.rows.cache import RefreshQueue, RowCache
from usher.telemetry import configure_telemetry, register_push_gauges, register_sse_gauge


class UsherAPI(FastAPI):
    """`FastAPI` with one override.

    `/openapi.json` tells the truth about the media type of a problem document.

    A subclass, not `app.openapi = custom`: the assignment is a mypy
    `method-assign` error here, and a replacement would have to re-implement
    the caching and `_openapi_routes_version` invalidation `FastAPI.openapi`
    already does. Delegating to `super()` keeps both.

    Not rewritten eagerly in the factory: that would make every `create_app()`
    pay for a schema most callers never read, and turn a schema-generation
    failure into a failure to boot.
    """

    def openapi(self) -> dict[str, Any]:
        return problem_responses_carry_their_media_type(super().openapi())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        session_factory = build_session_factory(engine)
        app.state.session_factory = session_factory
        # The outbound rate gates, and the one place they can live.
        gates = source_gates(settings)
        app.state.source_gates = gates
        # The TMDb provider's token bucket must be process-scoped (`api/deps.py`
        # says why), and the worker lane is the only thing here that needs it.
        provider, close_provider = (
            await metadata_provider(settings) if settings.worker_enabled else (None, nothing)
        )
        model, close_model = await embedder(settings, report=settings.worker_enabled)
        # On `app.state` and not held only by `LaneSupervisor`: `deps.get_search_service`
        # reads it here, which is what makes `?mode=semantic` and the vector half of
        # `?mode=fused` reachable from the HTTP surface at all.
        app.state.embedder = model
        # One per process, built only where a worker will use it. The shipped
        # `USHER_LLM_ENABLED=false` answers `(None, no-op)`, leaving `JobKind.CURATE`
        # unregistered -- a push-only deployment holds no client with no reader.
        client, close_client = (
            await llm_client(settings) if settings.worker_enabled else (None, nothing)
        )
        # `GET /images/{id}`'s two process-scoped halves.
        image_fetcher, image_store, close_images = image_proxy(settings)
        app.state.image_fetcher = image_fetcher
        app.state.image_store = image_store
        # PRD 10's keystroke rows. One buffer and one drain per process, so a row a
        # keystroke submits is written by a task the answered request no longer waits on.
        search_queries = search_query_buffer(session_factory)
        app.state.search_queries = search_queries
        draining = asyncio.create_task(search_queries.drain())
        lanes = LaneSupervisor(
            settings,
            unit_of_work(session_factory, settings, events=bus, provider=provider, gates=gates),
            bus,
            user_id=DefaultUserId(session_factory),
            provider=provider,
            embedder=model,
            client=client,
            rows=row_cache,
            refreshes=row_refreshes,
            # A retention prune has no use for a `Pipeline`, so the scheduler
            # reaches a database through this rather than `unit_of_work` above.
            # `LaneSupervisor` holds it opaquely, so this module stays the only
            # one here that knows what an engine is.
            sessions=session_factory,
        )
        app.state.lanes = lanes
        # PRD 10's `usher.source.push.connected` / `.reconnects`. Registered
        # unconditionally: "no lane, no observation" keeps a push-disabled process
        # from reporting a fabricated zero on a series whose alert fires on it.
        register_push_gauges(lanes.push_snapshots)
        # Opens no connection -- that is what keeps `/health` answering 200
        # with Postgres down.
        await lanes.start()
        # The `try:` opens here and not at the engine, so a raise from
        # `metadata_provider`, `embedder`, `llm_client` or `lanes.start()` leaks
        # whatever was already built.
        try:
            yield
        finally:
            # A bare `yield` with no try/finally skips this entirely when the
            # lifespan task is cancelled while suspended at the yield -- exactly
            # the shape ASGI shutdown uses.
            await lanes.stop()
            # Told to stop, never cancelled: `CancelledError` is not an
            # `Exception`, so a cancel landing inside the write escapes the
            # guard that absorbs everything else and rolls that batch back.
            await search_queries.aclose()
            with suppress(asyncio.CancelledError):
                await draining
            await close_provider()
            await close_model()
            await close_client()
            await close_images()
            await engine.dispose()

    app = UsherAPI(
        title="Usher",
        version="0.1.0",
        description="A self-hosted media catalog backend.",
        lifespan=lifespan,
    )
    # Gives every request a real server span even with no OTLP collector, so logs
    # have something to correlate against and explicit pipeline spans nest under a
    # request trace instead of each becoming its own root.
    FastAPIInstrumentor.instrument_app(app)
    # …and this is what lets that span leave the process.
    app.add_middleware(TraceResponseMiddleware)
    # Set here and not in the lifespan: settings are not a resource with a
    # lifetime, and `create_app(settings)`'s whole point is that the app runs on
    # what it was handed, not on the environment at the moment a request arrives.
    app.state.settings = settings
    # The process-wide client event bus (PRD 07's SSE channel).
    bus = InMemoryEventBus(buffer_size=settings.sse_buffer_size, queue_size=settings.sse_queue_size)
    app.state.events = bus
    # PRD 10's `usher.sse.connections`, the one observable callback here that is a
    # live read rather than a snapshot -- `len()` on an in-memory set has no coroutine
    # to bounce onto the event loop from the metric reader's background thread.
    register_sse_gauge(lambda: bus.subscribers)
    # The process's row and screen caches (PRD 06).
    row_cache = RowCache(clock=lambda: datetime.now(UTC))
    app.state.row_cache = row_cache
    # PRD 06's "served stale while refreshing": the handover between a request that
    # found a screen inside its grace window and the one lane that replaces it.
    row_refreshes = RefreshQueue()
    app.state.row_refreshes = row_refreshes
    # Replaces FastAPI's default 422 body, which echoes the submitted request -- and
    # `POST /admin/sources` submits a source credential.
    app.add_exception_handler(RequestValidationError, validation_error_without_the_request_body)
    # **Starlette's** `HTTPException`, not FastAPI's subclass, so the unrouted 404
    # and the 405 the router raises before any handler runs answer the same envelope
    # as the ones handlers raise. On the app, so a route added later inherits it.
    app.add_exception_handler(StarletteHTTPException, http_error_as_a_problem_document)
    # The two port failures no route can answer better than the app can. By exact
    # type rather than on `UsherPortError`, because `PortUnavailable` genuinely means
    # different things to different routes -- `routers/rows.py` wants its 500.
    app.add_exception_handler(PortRateLimited, port_error_as_a_problem_document)
    app.add_exception_handler(PortAuthFailed, port_error_as_a_problem_document)
    app.include_router(bootstrap.router)
    app.include_router(browse.router)
    app.include_router(collections.router)
    app.include_router(events.router)
    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(images.router)
    app.include_router(meta.router)
    app.include_router(people.router)
    app.include_router(playback.router)
    app.include_router(rows.router)
    app.include_router(search.router)
    app.include_router(series.router)
    app.include_router(sources.router)
    app.include_router(titles.router)
    app.include_router(unmatched.router)
    app.include_router(watch.router)
    # **Last, and that ordering is the whole safety argument.** The console's history
    # fallback answers any navigation-shaped miss under `/console` -- Starlette matches
    # in registration order, so a miss outside it still falls through to the 404.
    mount_console(app, settings)
    return app
