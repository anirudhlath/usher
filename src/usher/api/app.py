"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.exceptions import HTTPException as StarletteHTTPException

from usher.api.console import mount_console
from usher.api.errors import (
    http_error_as_a_problem_document,
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
    unit_of_work,
)
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.services.events import InMemoryEventBus
from usher.services.rows.cache import RefreshQueue, RowCache
from usher.telemetry import configure_telemetry, register_push_gauges, register_sse_gauge


class UsherAPI(FastAPI):
    """`FastAPI` with one override: `/openapi.json` tells the truth about the
    media type of a problem document.

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
        # The TMDb provider, and the one place its token bucket can live:
        # `api/deps.py` says why it cannot be request-scoped ("N in-flight
        # requests get N x 30 rps"), and the worker lane is the only thing
        # in this process that needs it. Not built at all when no worker
        # runs here -- an idle `httpx.AsyncClient` in a push-only
        # deployment is a resource with no reader.
        provider, close_provider = (
            await metadata_provider(settings) if settings.worker_enabled else (None, nothing)
        )
        # The embedding model. **One per process, and -- unlike the provider
        # above and the client below -- built whatever the lane switches say
        # (issue #31).** The other two are worker capabilities; this one has a
        # second reader on a request path, because `api/deps.get_search_service`
        # hands it to `SearchService` and `?mode=semantic` is unservable
        # without it. Gated on `worker_enabled` it was absent from exactly the
        # deployment `.claude/rules/api-telemetry-and-lanes.md` recommends -- a
        # server beside a `usher work` container, `USHER_WORKER_ENABLED=false`
        # on the server -- which then had a configured model, a backfilled
        # catalog, and a 422 on every semantic search.
        #
        # **Nothing had to be decoupled to do it: `composition.embedder`
        # already answers `(None, no-op)` unless `embedding_enabled`**, which
        # is `false` by default and is an operator naming a model. So the lane
        # switch was standing in for a setting that says the same thing more
        # precisely, and the deployment that now pays something it did not --
        # `embedding_enabled` on, `worker_enabled` off, the `fastembed:`
        # runtime -- pays a 65 MB / 4.84 s load at startup for the capability
        # it configured a model to get. On the `openai:` runtime it is an
        # `httpx.AsyncClient`.
        #
        # `report=` is the lane switch's remaining job, and it is the whole of
        # it: `embedder`'s warnings all end *"index jobs will not be claimed"*,
        # which is true of a process running the worker lane and false of one
        # that claims nothing. Silence is right there -- what a push-only
        # deployment loses is legible on the wire instead, as the 422 naming
        # the missing capability.
        model, close_model = await embedder(settings, report=settings.worker_enabled)
        # **Parked, and that is the line issue #31 is about.** Held only by
        # `LaneSupervisor` it is a process resource with one reader; on
        # `app.state` it is the one `api/deps.get_search_service` reads, which
        # is what makes `?mode=semantic` and the vector half of `?mode=fused`
        # reachable from the HTTP surface at all. `None` here is not an
        # absence to be guarded against -- it is `build_search_service`'s own
        # default for the parameter, so a deployment with no model serves
        # exactly what it served before.
        app.state.embedder = model
        # The completion client, on the same terms again: one per process,
        # built only where a worker will use it. `USHER_LLM_ENABLED=false` is
        # the shipped default and answers `(None, no-op)`, which is what
        # leaves `JobKind.CURATE` unregistered -- so a push-only or
        # LLM-less deployment holds no `httpx.AsyncClient` with no reader.
        client, close_client = (
            await llm_client(settings) if settings.worker_enabled else (None, nothing)
        )
        # `GET /images/{id}`'s two process-scoped halves. **Unconditional,
        # unlike the three above**: those are worker capabilities and answer
        # `(None, no-op)` where no worker runs here, but this one is on a
        # request path that every deployment serves. There is no switch and
        # nothing to be missing -- the CDN needs no credential (ADR-0032) and
        # both inputs have defaults -- so a nullable here would be a
        # degradation nothing can cause. One `httpx.AsyncClient` per process
        # for `metadata_provider`'s reason: a client per request is a
        # connection pool per request.
        image_fetcher, image_store, close_images = image_proxy(settings)
        app.state.image_fetcher = image_fetcher
        app.state.image_store = image_store
        lanes = LaneSupervisor(
            settings,
            unit_of_work(session_factory, settings, events=bus, provider=provider),
            bus,
            user_id=DefaultUserId(session_factory),
            provider=provider,
            embedder=model,
            client=client,
            rows=row_cache,
            refreshes=row_refreshes,
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
        # `metadata_provider`, `embedder`, `llm_client` or `lanes.start()`
        # leaks whatever was already built.** Three resources now instead of
        # M5's two, so the window widened by one this milestone. Measured and
        # left alone rather than overlooked: each of those four raises only
        # on a misconfiguration this process cannot survive anyway (a bad
        # DSN, a missing ONNX model, an unusable base URL), the process exits
        # seconds later, and the operating system reclaims the socket and the
        # mapping. `contextlib.AsyncExitStack` is the fix if any of the four
        # ever becomes recoverable -- push each closer as it is built and let
        # the stack unwind in reverse -- and `usher work`'s `finally`
        # (`cli._work`) has the same shape and would take the same change.
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
            await close_client()
            await close_images()
            await engine.dispose()

    app = UsherAPI(
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
    # …and this is what lets that span leave the process. `traceresponse` on
    # every response with a live span, so `Problem`'s "Open trace" — the single
    # link `web/docs/patterns.md` §3 says separates a console from a settings
    # page — has an id to build a Tempo URL from. **Added after the
    # instrumentor on purpose**: `instrument_app` rebuilds the whole middleware
    # stack around this one, and reading `trace.get_current_span()` from the
    # user-middleware slot is only the *server* span because
    # `OpenTelemetryMiddleware` ends up outside it. `api/trace_response.py`
    # carries the measured stack and the one response this does not reach.
    app.add_middleware(TraceResponseMiddleware)
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
    # PRD 06's "served stale while refreshing": the handover between a request
    # that found a screen inside its grace window and the one lane that
    # replaces it. Here rather than in the lifespan on exactly the terms above
    # -- it is a bounded dict-and-deque, not a resource with a lifetime -- and
    # **one per app, never per request**, since a request-scoped queue would
    # deduplicate nothing and be drained by nobody. Built before `lanes` for
    # the same reason `row_cache` is: the lifespan closes over both.
    row_refreshes = RefreshQueue()
    app.state.row_refreshes = row_refreshes
    # Replaces FastAPI's default 422 body, which echoes the submitted
    # request -- and `POST /admin/sources` submits a source credential. See
    # usher.api.errors; this is a security control, not a response-shape
    # preference, and it is registered here so it covers every route rather
    # than only the one that made it necessary. PRD 07's RFC 9457 envelope
    # now wraps it, and composes rather than replaces: the stripped error
    # list rides as an extension member and `detail` is a fixed sentence.
    app.add_exception_handler(RequestValidationError, validation_error_without_the_request_body)
    # **Starlette's** `HTTPException`, not FastAPI's subclass, so the two the
    # router raises before any handler runs -- an unrouted 404 and a 405 --
    # answer the same envelope as the ones handlers raise. Registered on the
    # app for the reason above: a route added later inherits the shape
    # instead of having to remember it.
    app.add_exception_handler(StarletteHTTPException, http_error_as_a_problem_document)
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
    # **Last, and that ordering is the whole safety argument.** The console's
    # mount answers `/console/*` and its history fallback answers any
    # navigation-shaped miss underneath it -- but Starlette matches routes in
    # registration order, so every route above is reached first and an
    # unrouted path outside `/console` still falls through to
    # `http_error_as_a_problem_document`'s 404. Mounting before the routers
    # would not shadow them either (the mount's own path is a prefix match on
    # `/console`), but the register-last rule is what keeps that true when
    # someone adds the next route.
    mount_console(app, settings)
    return app
