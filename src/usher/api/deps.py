"""Request-scoped dependencies, and the API's composition root.

`api/` is allowed to import `adapters/` and `db/` -- that is what a
composition root does. The import-linter contracts forbid only
`domain`/`ports`/`services` from reaching either, plus (contract six) any
direct naming of a *concrete* adapter, which is why the factory below is
`ConfiguredSourceAdapterFactory` and not `EmbyAdapter`.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.lanes import LaneSupervisor
from usher.composition import adapter_factory
from usher.config import Settings
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresRawPayloadStore, PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import ensure_default_user
from usher.ports.events import EventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.repository import (
    EpisodeRepository,
    MediaItemRepository,
    RawPayloadStore,
    SourceRepository,
    SyncRunRepository,
    TitleMatchRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.source import SourceAdapterFactory
from usher.services.events import InMemoryEventBus
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.sources import SourceService
from usher.services.titles import TitleReadService
from usher.services.watch_sync import WatchStateSyncService


def get_app_settings(request: Request) -> Settings:
    """The settings this app was *built* with, off `app.state`.

    Deliberately not `usher.config.get_settings`, even though that is
    cached and exists to be a `Depends`. `create_app(settings)` takes an
    explicit `Settings` and uses it for the engine and for telemetry, so a
    dependency that re-read the environment instead would hand handlers a
    *different* configuration than the one the app is running on -- silently
    in production (where both usually agree) and fatally under test, where
    `tests/conftest.py` strips every `USHER_*` variable and a bare
    `Settings()` cannot validate at all. Verified directly: with
    `Depends(get_settings)`, `POST /admin/sources` 500s in the integration
    suite on a missing `database_url`.

    Same defensive shape as `get_session_factory` below, and for the same
    reason: `app.state` is typed `Any`, so without the `cast` mypy would
    accept this returning anything at all.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError(
            "app.state.settings is not set -- this app was not built by "
            "usher.api.app.create_app, which is the only thing that sets it."
        )
    return cast(Settings, settings)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_event_bus(request: Request) -> InMemoryEventBus:
    """The process-wide client event bus, built by `create_app`'s lifespan.

    On `app.state` rather than request-scoped for the reason `EnrichService`
    is absent from this module: a per-request bus would give every SSE
    connection its own, and a publisher would fan out to nobody. Same
    defensive `getattr`/`cast` shape as `get_session_factory` below, and for
    the same reason -- `app.state` is typed `Any`.

    Typed as the concrete bus rather than as `EventPublisher` because
    `GET /events` needs `subscribe`, which is deliberately not on the port
    (a `LISTEN/NOTIFY` implementation subscribes on a dedicated connection
    whose lifecycle has nothing in common with an in-memory queue's). Use
    `get_event_publisher` anywhere that only publishes, so nothing but this
    one function depends on the wider surface.
    """
    bus = getattr(request.app.state, "events", None)
    if bus is None:
        raise RuntimeError(
            "app.state.events is not set -- create_app's lifespan has not run. "
            "If this is a test using a bare ASGI transport, wrap the app in "
            "asgi_lifespan.LifespanManager first."
        )
    return cast(InMemoryEventBus, bus)


EventBusDep = Annotated[InMemoryEventBus, Depends(get_event_bus)]


def get_event_publisher(bus: EventBusDep) -> EventPublisher:
    """The same object, as the port.

    Routes and services that only publish take this, so nothing outside
    `get_event_bus` depends on the bus offering `subscribe`.
    """
    return bus


EventPublisherDep = Annotated[EventPublisher, Depends(get_event_publisher)]


def get_lane_supervisor(request: Request) -> LaneSupervisor:
    """The process's background lanes, started by `create_app`'s lifespan.

    Read by `/health/ready`, which **reports** what it finds here and never
    gates its status code on it, and by `GET /admin/sources/{id}/status`,
    which takes the *running lane's* push health rather than opening a
    socket of its own. Same defensive `getattr`/`cast` shape as
    `get_session_factory` below, and for the same reason -- `app.state` is
    typed `Any`, so without the `cast` mypy would accept this returning
    anything at all.
    """
    lanes = getattr(request.app.state, "lanes", None)
    if lanes is None:
        raise RuntimeError(
            "app.state.lanes is not set -- create_app's lifespan has not run. "
            "If this is a test using a bare ASGI transport, wrap the app in "
            "asgi_lifespan.LifespanManager first."
        )
    return cast(LaneSupervisor, lanes)


LaneSupervisorDep = Annotated[LaneSupervisor, Depends(get_lane_supervisor)]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Typed accessor for the session factory `create_app`'s lifespan
    installs on `app.state`.

    `request.app.state.session_factory` is otherwise typed `Any` --
    Starlette's `State` permits arbitrary attributes, so `get_session`'s
    `AsyncIterator[AsyncSession]` return type was previously unverified by
    mypy despite strict mode passing clean: it would have accepted
    `session_factory` being anything at all. Raises a diagnosable
    `RuntimeError` instead of Starlette's generic `AttributeError:
    'State' object has no attribute 'session_factory'` if this is ever
    reached before the lifespan has run -- exactly what a bare
    `httpx.ASGITransport` without `asgi_lifespan.LifespanManager` produced
    before `tests/integration/test_health.py`'s fixture was fixed.
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError(
            "app.state.session_factory is not set -- create_app's lifespan has "
            "not run. If this is a test using httpx.ASGITransport directly, "
            "wrap the app in asgi_lifespan.LifespanManager first."
        )
    return cast(async_sessionmaker[AsyncSession], factory)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session and the request's unit-of-work boundary:
    commits once the handler completes without raising, rolls back and
    re-raises otherwise.

    `ports/repository.py` says "the caller owns the session and the
    transaction... committing or rolling back is the caller's call" --
    ambiguous about who "the caller" is once a repository sits behind a
    request handler behind a dependency. This makes it concrete:
    repositories flush, this commits. Without it, nothing in `src/` ever
    called `commit()` at all -- `AsyncSession.close()` (which `async with
    factory() as session` calls on exit) silently discards an open
    transaction, so a write endpoint that forgot to commit would lose
    data with no error and no log.
    """
    factory = get_session_factory(request)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_default_user_id(session: SessionDep) -> uuid.UUID:
    """The singleton `is_default` user's id, creating the row on first use.

    `usher.db.users.ensure_default_user` used to be called from `usher.cli`
    and nowhere else, so a deployment that only ever ran the server -- which
    is exactly what the container's `CMD` does -- had an empty `users`
    table, and `watch_states.user_id` is a real foreign key. Unreachable in
    M4, because no route writes a watch state; M5's push and reconnect-delta
    routes are precisely the ones that do.

    **Request-scoped rather than a lifespan call, deliberately.**
    `create_app`'s lifespan builds an engine and opens no connection, and
    that is load-bearing: `/health` keeps answering 200 with Postgres down
    while `/health/ready` reports 503, verified live against a real
    container (PRD 08). A write at startup would turn a database outage into
    a crash loop and an unmigrated schema into a failure to boot -- trading a
    documented, tested degradation for a worse one, and for a row that is
    only ever needed by a request. Here it costs one `SELECT` on the request
    that needs it and one `INSERT` on the first such request ever, inside
    that request's own transaction, committed by `get_session`.

    Nothing routes over this yet, for the same reason nothing routes over
    the pipeline services above: the surface is M5's and M9's. It is wired
    and tested now so the milestone that adds those routes is adding
    routes, not discovering wiring
    (`tests/integration/test_pipeline_deps.py`).
    """
    return await ensure_default_user(session)


DefaultUserIdDep = Annotated[uuid.UUID, Depends(get_default_user_id)]


def get_source_repository(session: SessionDep) -> SourceRepository:
    """Its own provider rather than being constructed inside
    `get_source_service`, because `get_title_read_service` needs the same one.

    Two callers each building their own would be two chances for one of them
    to drift onto a different session and quietly leave the request's
    transaction -- the failure `tests/integration/test_pipeline_deps.py`
    exists to make observable. Declared here, above its first user, because
    `Depends(...)` is evaluated when the `def` below executes.
    """
    return PostgresSourceRepository(session)


def get_source_adapter_factory(settings: SettingsDep) -> SourceAdapterFactory:
    """The composition root's adapter registry.

    Its own dependency, not inlined into `get_source_service`, so a test can
    override exactly this one thing -- pointing the real `EmbyAdapter` at an
    in-memory server -- without also replacing the repository, the
    credential store, or the service.
    """
    return adapter_factory(settings)


def get_source_service(
    session: SessionDep,
    settings: SettingsDep,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    adapters: Annotated[SourceAdapterFactory, Depends(get_source_adapter_factory)],
) -> SourceService:
    return SourceService(
        sources,
        PostgresCredentialStore(session, settings.secret_key),
        adapters,
    )


SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]


# ---------------------------------------------------------------------------
# The ingest pipeline (M4).
#
# PRD 07's `POST /admin/sources/{id}/sync` and the two `/admin/unmatched`
# routes are M9's surface, so nothing here is routed over yet. It exists
# because a composition root is the thing that has to agree with the other
# one: `usher.cli` wires the identical graph, and a second root that had
# never been written would let M9 discover at route-writing time that a
# service needs something a request scope cannot give it. Every provider
# below is exercised by `tests/integration/test_pipeline_deps.py`, which
# resolves each one through FastAPI's own dependency machinery rather than
# by calling the functions -- an unresolvable `Depends` graph is a startup
# error a plain call cannot produce.
#
# Return types are the *ports*, not the `Postgres*` classes, so a route
# written against one of these annotations cannot reach a method the port
# does not have.
# ---------------------------------------------------------------------------


def get_title_repository(session: SessionDep) -> TitleRepository:
    return PostgresTitleRepository(session)


def get_title_match_repository(session: SessionDep) -> TitleMatchRepository:
    return PostgresTitleMatchRepository(session)


def get_media_item_repository(session: SessionDep) -> MediaItemRepository:
    return PostgresMediaItemRepository(session)


def get_episode_repository(session: SessionDep) -> EpisodeRepository:
    return PostgresEpisodeRepository(session)


def get_watch_state_repository(session: SessionDep) -> WatchStateRepository:
    return PostgresWatchStateRepository(session)


def get_sync_run_repository(session: SessionDep) -> SyncRunRepository:
    return PostgresSyncRunRepository(session)


def get_raw_payload_store(session: SessionDep) -> RawPayloadStore:
    return PostgresRawPayloadStore(session)


def get_job_queue(session: SessionDep, settings: SettingsDep) -> JobQueue:
    return PostgresJobQueue(
        session,
        max_attempts=settings.job_max_attempts,
        backoff_seconds=settings.job_backoff_seconds,
    )


MediaItemRepositoryDep = Annotated[MediaItemRepository, Depends(get_media_item_repository)]
SyncRunRepositoryDep = Annotated[SyncRunRepository, Depends(get_sync_run_repository)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]


def get_match_service(
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
    matching: Annotated[TitleMatchRepository, Depends(get_title_match_repository)],
    queue: JobQueueDep,
) -> MatchService:
    """The five-tier matcher, with **no** metadata provider.

    Not an omission. `MatchService.match` runs inside a walk and its
    constructor takes the provider as optional precisely so the batch path
    cannot make a network call per unmatched item; only `match_remote` --
    the queued `match` handler's entry point, which `usher work` runs --
    needs one. A request-scoped provider would also give every request its
    own token bucket, which is a rate limiter that limits nothing: see
    `usher.cli._work`, where the one client that owns the bucket lives.
    """
    return MatchService(titles=titles, matching=matching, queue=queue)


def get_ingest_service(
    matcher: Annotated[MatchService, Depends(get_match_service)],
    matching: Annotated[TitleMatchRepository, Depends(get_title_match_repository)],
    media_items: MediaItemRepositoryDep,
    episodes: Annotated[EpisodeRepository, Depends(get_episode_repository)],
    queue: JobQueueDep,
) -> IngestService:
    return IngestService(
        matcher=matcher,
        matching=matching,
        media_items=media_items,
        episodes=episodes,
        queue=queue,
    )


def get_reconcile_service(
    session: SessionDep,
    settings: SettingsDep,
    ingest: Annotated[IngestService, Depends(get_ingest_service)],
    media_items: MediaItemRepositoryDep,
    runs: SyncRunRepositoryDep,
    events: EventPublisherDep,
) -> ReconcileService:
    """`commit` is `session.commit`, the same callable `get_session` calls
    at the end of a successful request.

    That is deliberate and it is the one place this root differs from the
    CLI's: a reconcile checkpoints and commits *per batch*, so a route that
    drove a six-hour walk inside one request would be committing the
    request's session repeatedly before the handler returned. M9 will run
    this on a background task rather than inline for exactly that reason --
    recorded here because the wiring is what makes it look possible.
    """
    return ReconcileService(
        ingest=ingest,
        media_items=media_items,
        runs=runs,
        events=events,
        commit=session.commit,
        batch_size=settings.sync_batch_size,
        max_retract_fraction=settings.sync_max_retract_fraction,
    )


def get_watch_state_sync_service(
    session: SessionDep,
    settings: SettingsDep,
    media_items: MediaItemRepositoryDep,
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    runs: SyncRunRepositoryDep,
    queue: JobQueueDep,
) -> WatchStateSyncService:
    return WatchStateSyncService(
        media_items=media_items,
        watch_states=watch_states,
        runs=runs,
        queue=queue,
        commit=session.commit,
        batch_size=settings.sync_batch_size,
    )


# `EnrichService` is deliberately absent, and this is the one place the plan
# was wrong rather than incomplete. It needs a `MetadataProvider`, whose only
# implementation owns the token bucket that keeps this deployment under
# TMDb's ~40 rps ceiling -- and a request-scoped `TmdbClient` gives every
# concurrent request a *fresh* bucket, so N in-flight requests get N x 30
# rps. The bucket has to outlive a request, which makes it a lifespan
# resource on `app.state` rather than a `Depends`, and nothing in PRD 07's
# surface calls enrichment directly (M5's demand promotion enqueues a job;
# `usher work` runs it). Adding the provider here would be wiring a rate
# limiter to be bypassed.
IngestServiceDep = Annotated[IngestService, Depends(get_ingest_service)]
ReconcileServiceDep = Annotated[ReconcileService, Depends(get_reconcile_service)]
WatchStateSyncServiceDep = Annotated[WatchStateSyncService, Depends(get_watch_state_sync_service)]


# ---------------------------------------------------------------------------
# The read-through surface (M5). `GET /titles/{id}` is the one route that
# routes over any of the providers above.
# ---------------------------------------------------------------------------


def get_title_read_service(
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
    media_items: MediaItemRepositoryDep,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    queue: JobQueueDep,
) -> TitleReadService:
    """Four repositories and the queue, and deliberately no adapter factory.

    The absence is the design (PRD 08: "a degraded subsystem narrows
    functionality; it never fails a request local state can answer"), not an
    omission that a later route should fill in: with no `SourceAdapter` in the
    graph there is no path from an unreachable Emby to a failed title read,
    and therefore no 503 for M5 to invent an error `code` for.
    `tests/unit/test_services_titles.py` asserts it on the service's own
    imports so that adding one here would fail rather than pass review.
    """
    return TitleReadService(titles, media_items, sources, watch_states, queue)


TitleReadServiceDep = Annotated[TitleReadService, Depends(get_title_read_service)]
