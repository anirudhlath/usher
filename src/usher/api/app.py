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

    A subclass rather than `app.openapi = …`, which is the spelling FastAPI's
    own "Extending OpenAPI" page shows. Two reasons, the first measured:
    `app.openapi = custom` is `error: Cannot assign to a method
    [method-assign]` under this project's mypy settings and would need the
    only `type: ignore` in `src/usher/api/`; and a replacement function has to
    re-implement the caching *and* the `_openapi_routes_version` invalidation
    `FastAPI.openapi` has since grown, which is a copy that goes silently
    wrong the day either changes. Delegating to `super()` keeps both and costs
    one idempotent walk of a 35-operation document per call.

    **Deliberately not an eager rewrite of `app.openapi_schema` in the
    factory.** Generating the document at build time would make every
    `create_app()` in the suite pay for a schema no case reads, and would turn
    a schema-generation failure into a failure to boot.
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
        # The outbound rate gates, and the one place they can live (ADR-0043 §4).
        gates = source_gates(settings)
        app.state.source_gates = gates
        # The TMDb provider, and the one place its token bucket can live: `api/deps.py`
        # says why it cannot be request-scoped ("N in-flight requests get N x 30 rps"),
        # and the worker lane is the only thing in this process that needs it.
        provider, close_provider = (
            await metadata_provider(settings) if settings.worker_enabled else (None, nothing)
        )
        # The embedding model.
        model, close_model = await embedder(settings, report=settings.worker_enabled)
        # **Parked, and that is the line issue #31 is about.** Held only by
        # `LaneSupervisor` it is a process resource with one reader; on `app.state` it
        # is the one `api/deps.get_search_service` reads, which is what makes
        # `?mode=semantic` and the vector half of `?mode=fused` reachable from the HTTP
        # surface at all.
        app.state.embedder = model
        # The completion client, on the same terms again: one per process,
        # built only where a worker will use it. `USHER_LLM_ENABLED=false` is
        # the shipped default and answers `(None, no-op)`, which is what
        # leaves `JobKind.CURATE` unregistered -- so a push-only or
        # LLM-less deployment holds no `httpx.AsyncClient` with no reader.
        client, close_client = (
            await llm_client(settings) if settings.worker_enabled else (None, nothing)
        )
        # `GET /images/{id}`'s two process-scoped halves.
        image_fetcher, image_store, close_images = image_proxy(settings)
        app.state.image_fetcher = image_fetcher
        app.state.image_store = image_store
        # PRD 10's keystroke rows. One buffer and one drain per process, so a
        # row a keystroke submits is written by a task the answered request is
        # no longer waiting on. Unconditional on `app.state.embedder`'s terms:
        # a buffer nobody submits to holds a deque and a parked task.
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
            # The scheduler's registrations reach a database through this and
            # not through `unit_of_work` above -- a retention prune has no use
            # for a `Pipeline`. `LaneSupervisor` holds it opaquely
            # (`SessionFactory`), so this module is still the only one here
            # that knows what an engine is.
            sessions=session_factory,
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
        # **The `try:` opens here rather than at the engine, so a raise from
        # `metadata_provider`, `embedder`, `llm_client` or `lanes.start()` leaks
        # whatever was already built.** Three resources now instead of M5's two, so the
        # window widened by one this milestone.
        try:
            yield
        finally:
            # Not just hygiene: verified directly that a bare `yield` with no
            # try/finally skips this call entirely if the task running the lifespan is
            # cancelled while suspended at yield (as opposed to __aexit__ being called
            # normally) -- exactly the shape ASGI shutdown uses.
            await lanes.stop()
            # Told to stop, never cancelled: `CancelledError` is not an
            # `Exception`, so a cancel landing inside the write escapes the
            # guard that absorbs everything else and rolls that batch back.
            # `aclose` flushes, and `flush` waits for a batch already in flight.
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
    # Gives every request a real server span (a valid trace/span id, even with no OTLP
    # collector configured -- see configure_tracing) so inject_trace_context has
    # something to correlate logs against, and so later milestones' explicit pipeline
    # spans nest under a request trace instead of each becoming its own root.
    FastAPIInstrumentor.instrument_app(app)
    # …and this is what lets that span leave the process.
    app.add_middleware(TraceResponseMiddleware)
    # The configuration handlers read, via `deps.get_app_settings`. Set here
    # rather than in the lifespan because it is not a resource with a
    # lifetime -- and because `create_app(settings)`'s whole point is that
    # the app runs on the settings it was handed, not on whatever the
    # environment says at the moment a request arrives.
    app.state.settings = settings
    # The process-wide client event bus (PRD 07's SSE channel).
    bus = InMemoryEventBus(buffer_size=settings.sse_buffer_size, queue_size=settings.sse_queue_size)
    app.state.events = bus
    # PRD 10's `usher.sse.connections`, and the one observable callback in this project
    # that is a live read rather than a snapshot -- `len()` on an in-memory set has no
    # coroutine to bounce onto the event loop from the metric reader's background
    # thread.
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
    # **Starlette's** `HTTPException`, not FastAPI's subclass, so the two the
    # router raises before any handler runs -- an unrouted 404 and a 405 --
    # answer the same envelope as the ones handlers raise. Registered on the
    # app for the reason above: a route added later inherits the shape
    # instead of having to remember it.
    app.add_exception_handler(StarletteHTTPException, http_error_as_a_problem_document)
    # The two port failures no route can answer better than the app can.
    # Registered by exact type rather than on `UsherPortError`, because
    # `PortUnavailable` genuinely means different things to different routes --
    # `routers/rows.py` wants the 500 it gets today. See `api/errors.py`.
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
    # **Last, and that ordering is the whole safety argument.** The console's mount
    # answers `/console/*` and its history fallback answers any navigation-shaped miss
    # underneath it -- but Starlette matches routes in registration order, so every
    # route above is reached first and an unrouted path outside `/console` still falls
    # through to `http_error_as_a_problem_document`'s 404.
    mount_console(app, settings)
    return app
