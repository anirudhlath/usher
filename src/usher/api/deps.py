"""Request-scoped dependencies, and the API's composition root.

`api/` is allowed to import `adapters/` and `db/` -- that is what a
composition root does. The import-linter contracts forbid only
`domain`/`ports`/`services` from reaching either, plus (contract six) any
direct naming of a *concrete* adapter, which is why the factory below is
`ConfiguredSourceAdapterFactory` and not `EmbyAdapter`.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.lanes import LaneSupervisor
from usher.composition import adapter_factory
from usher.config import Settings
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.search import (
    PostgresTitleEmbeddingRepository,
    PostgresTitleNeighborRepository,
)
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresRawPayloadStore, PostgresSyncRunRepository
from usher.db.repositories.taste import PostgresTasteRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import default_user, ensure_default_user
from usher.domain.taste import GenreAffinity
from usher.domain.watch import User
from usher.ports.events import EventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.repository import (
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    MediaItemRepository,
    PersonRepository,
    RawPayloadStore,
    SourceRepository,
    SyncRunRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleMatchRepository,
    TitleNeighborRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.rows import RowContext
from usher.ports.source import SourceAdapterFactory
from usher.services.events import InMemoryEventBus
from usher.services.home import HomeService
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.rows.cache import RefreshQueue, RowCache
from usher.services.sources import SourceService
from usher.services.taste import TasteService
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
    lanes: LaneSupervisorDep,
) -> SourceService:
    """The service, plus the *running lane's* push health.

    `SourceStatus.push_available` is never a probe of a throwaway socket
    (ADR-0004: a handshake against a nonexistent path also upgrades, so the
    handshake is not the answer). `verify()` opens none, and this is what
    fills the gap: the lane's own adapter holds a message ledger, and its
    answer is the one an operator reads. `None` when no lane is running for
    that source, which is "not probed" rather than "push is broken".
    """
    return SourceService(
        sources,
        PostgresCredentialStore(session, settings.secret_key),
        adapters,
        lanes.push_available,
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


# ---------------------------------------------------------------------------
# The composed home screen (M7). `GET /home` is the first client-facing route
# since M5, and ADR-0006 is why: "one request paints a screen" is a property of
# a request boundary, which no CLI can exhibit.
#
# **Each provider is declared above its first user.** `Depends(...)` is
# evaluated when the `def` below it executes, so appending a provider after its
# consumer is a `NameError` at import of this module rather than a puzzle at
# request time.
# ---------------------------------------------------------------------------


def get_title_neighbor_repository(session: SessionDep) -> TitleNeighborRepository:
    return PostgresTitleNeighborRepository(session)


def get_title_embedding_repository(session: SessionDep) -> TitleEmbeddingRepository:
    return PostgresTitleEmbeddingRepository(session)


def get_person_repository(session: SessionDep) -> PersonRepository:
    return PostgresPersonRepository(session)


def get_credit_repository(session: SessionDep) -> CreditRepository:
    return PostgresCreditRepository(session)


def get_collection_repository(session: SessionDep) -> CollectionRepository:
    return PostgresCollectionRepository(session)


def get_taste_repository(session: SessionDep) -> TasteRepository:
    return PostgresTasteRepository(session)


def get_curated_row_repository(session: SessionDep) -> CuratedRowRepository:
    """The read half only, and that is what a request is allowed to have.

    `CuratedRowRepository` also carries `replace_for_user`, which is a
    *generation*: one paid completion, a validator, and a delete-then-insert
    over a household's whole screen. Nothing on this path may reach it -- the
    write belongs to `JobKind.CURATE` under `usher work`, and
    `POST /admin/rows/regenerate` enqueues that rather than doing it. The port
    is handed over whole because splitting a repository in two to express which
    half a caller uses is a second port for one table; what keeps the write off
    this path is that `CuratedProvider` is the only thing here that holds one
    and it calls `list_for_user`.
    """
    return PostgresCuratedRowRepository(session)


async def get_default_user(session: SessionDep) -> User:
    """The singleton default user as a **model**, not just an id.

    `RowContext` carries a `User`, and `User.id` is `default_factory=new_id` --
    so `User(name="default", is_default=True)` built here would compose a screen
    for a household that has never existed. Every read would return nothing and
    the screen would render empty, which is indistinguishable from a household
    that has watched nothing. That is this milestone's headline failure arriving
    through a constructor default, which is why the row is read.
    """
    return await default_user(session)


def get_taste_service(
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    embeddings: Annotated[TitleEmbeddingRepository, Depends(get_title_embedding_repository)],
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
    taste: Annotated[TasteRepository, Depends(get_taste_repository)],
) -> TasteService:
    """**No embedder, and that is the same call `get_home_service` makes.**

    `create_app`'s lifespan builds the model only when `worker_enabled`, so a
    request-scoped dependency that reached for one would work in development and
    500 in exactly the push-only deployment PRD 08 describes. It is also a
    once-per-*process* resource -- a 65 MB ONNX session and a 4.84 s cold load --
    which `deps.py` already argues about the TMDb token bucket one section up.

    What that costs, stated rather than hidden: `TasteService.centroid` returns
    `None` when there is no embedder, so `RowContext.taste` is `None` on every
    request. **No provider registered in M7 reads that field**, so nothing on
    the screen changes -- but a deployment whose worker *did* compute a centroid
    cannot serve it from here, and closing that is a change to `centroid`'s own
    contract rather than to this wiring. `genre_affinity` is unaffected: it is
    counts over `titles.genres` and needs no model at all, which is the whole
    reason M7 declined PRD 06's "taste centroid concentrated in a genre".
    """
    return TasteService(
        watch_states=watch_states,
        embeddings=embeddings,
        titles=titles,
        taste=taste,
        embedder=None,
        now=lambda: datetime.now(UTC),
    )


class _Affinities:
    """This household's genre affinities, read on demand and then remembered.

    **The whole point is that `__call__` may never run.** `RowContext`'s field
    used to be the awaited *value*, which put three statements --
    `list_recent(50)`, `list_by_ids(50)` and a library-wide `unnest(genres)
    GROUP BY` over 1.27M titles -- in front of `HomeService.compose_report`'s
    look in the ~30 s screen cache, on every request including the ones that
    hit. Deferred, a hit costs nothing and a miss costs exactly what it did.

    **Memoised because the field is a promise about the request, not about the
    reader.** `GenreAffinityProvider` is the only thing that awaits it today
    and awaits it once; a second reader tomorrow must not be a second read, and
    a provider cannot arrange that for itself because a context is frozen
    precisely so nothing can stash state on it between `propose` and `build`.
    One request, one answer, at most one read.

    **`_answer is None` is the miss test rather than falsiness**, because `[]`
    is the *common* real answer -- no genre cleared `_MIN_LIFT` and
    `_MIN_SUPPORT`, which is what most households produce -- and a memo that
    read falsiness would re-read on every ask for exactly the households with
    nothing to find. Same shape as `TasteService._engaged`'s own memo, one
    layer down, and stated in both places because both are one keystroke from
    the version that quietly does nothing.

    Not a closure, so the memo has a name a reader can find and the docstring
    has somewhere to live; `__slots__` because one is built per request.
    """

    __slots__ = ("_answer", "_taste", "_user_id")

    def __init__(self, taste: TasteService, user_id: uuid.UUID) -> None:
        self._taste = taste
        self._user_id = user_id
        self._answer: Sequence[GenreAffinity] | None = None

    async def __call__(self) -> Sequence[GenreAffinity]:
        if self._answer is None:
            self._answer = await self._taste.genre_affinity(self._user_id)
        return self._answer


async def get_row_context(
    user: Annotated[User, Depends(get_default_user)],
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
    media_items: MediaItemRepositoryDep,
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    episodes: Annotated[EpisodeRepository, Depends(get_episode_repository)],
    neighbors: Annotated[TitleNeighborRepository, Depends(get_title_neighbor_repository)],
    people: Annotated[PersonRepository, Depends(get_person_repository)],
    credits: Annotated[CreditRepository, Depends(get_credit_repository)],
    collections: Annotated[CollectionRepository, Depends(get_collection_repository)],
    curated: Annotated[CuratedRowRepository, Depends(get_curated_row_repository)],
    taste: Annotated[TasteService, Depends(get_taste_service)],
) -> RowContext:
    """The twelve values a row may reach, for one request, for one user.

    **`affinities` is a value the composer hands over, not a service a
    provider reaches.** A provider may import only `domain/` and `ports/`, so
    `TasteService` cannot appear on the context -- and recomputing the affinity
    inside a provider would need a `TasteRepository` field *and* a second copy
    of the lift arithmetic. `ports/rows.py` argues it at length.

    **It is handed over as a callable, and nothing in this function reads the
    household's taste.** `await taste.genre_affinity(user.id)` was evaluated
    here, which is *before* `HomeService.compose_report` can look in the screen
    cache -- FastAPI resolves the dependency graph and only then calls the
    handler -- so a 30 s cache hit, which is most requests, had already paid
    the three most expensive statements on the path. `_Affinities` defers them
    to `GenreAffinityProvider`'s own `await` and memoises the answer for the
    request.

    **`search` and `taste` used to be here and are not.** No provider read
    either, and `taste` cost a `user_taste` read on every request to deliver a
    value that is structurally `None` on this path -- `TasteService.centroid`
    returns `None` without an embedder and this route deliberately holds none.
    `TasteService` is still injected, for `genre_affinity`.

    **`curated` is M8's, and it is a repository on the same terms as the other
    nine.** `CuratedProvider` hydrates what a background job stored; nothing on
    this path generates anything, so `GET /home` acquires no `LLMClient`, no
    API key and no reason to 503 when the endpoint is down. A deployment with
    `USHER_LLM_ENABLED=false` reads an empty table and gets a home screen with
    fewer rows -- the same shape as a deployment with no embedder.

    **No `AsyncSession` here either**, which is the structural half of trap 4:
    a row holding repositories has no session to share, so there is nothing for
    a `gather` to interleave. That the repositories underneath share one is the
    composer's problem, stated once in `HomeService`.
    """
    return RowContext(
        user=user,
        # The wall clock, bound per request. `SeasonalProvider` fires on a
        # calendar window and `RediscoverProvider` on "watched > 2 years ago";
        # a fixture-friendly clock is exactly why this is a callable.
        now=lambda: datetime.now(UTC),
        titles=titles,
        media_items=media_items,
        watch_states=watch_states,
        episodes=episodes,
        neighbors=neighbors,
        people=people,
        credits=credits,
        collections=collections,
        affinities=_Affinities(taste, user.id),
        curated=curated,
    )


RowContextDep = Annotated[RowContext, Depends(get_row_context)]


def get_row_cache(request: Request) -> RowCache:
    """The process's one row cache, off `app.state`.

    On `app.state` rather than request-scoped for the reason the event bus is:
    **a request-scoped cache caches nothing**, exactly as a request-scoped bus
    fans out to nobody. Same defensive `getattr`/`cast` shape as
    `get_event_bus`, and for the same reason -- `app.state` is typed `Any`, so
    without it a missing lifespan is an `AttributeError` deep inside a handler
    rather than a sentence naming the cause.
    """
    cache = getattr(request.app.state, "row_cache", None)
    if not isinstance(cache, RowCache):
        raise RuntimeError(
            "app.state.row_cache is not set -- this app was not built by "
            "create_app, or its lifespan has not run."
        )
    return cache


def get_refresh_queue(request: Request) -> RefreshQueue:
    """The process's one stale-key queue, off `app.state`.

    Same lifetime and same defensive shape as `get_row_cache` above, and for a
    sharper version of the same reason: a request-scoped queue would
    deduplicate nothing (every request its own `pending` set) and would be
    drained by nobody, so serve-stale would degrade to serving stale forever
    -- silently, since the request still gets a screen.
    """
    queue = getattr(request.app.state, "row_refreshes", None)
    if not isinstance(queue, RefreshQueue):
        raise RuntimeError(
            "app.state.row_refreshes is not set -- this app was not built by "
            "create_app, or its lifespan has not run."
        )
    return queue


def get_home_service(
    cache: Annotated[RowCache, Depends(get_row_cache)],
    refreshes: Annotated[RefreshQueue, Depends(get_refresh_queue)],
) -> HomeService:
    """The composer, over the registry `services/rows/__init__.py` owns.

    The provider list is **not** assembled here: a list a composition root
    builds by hand is a list the tenth provider is forgotten from, which is dead
    code that looks exactly like a provider with nothing to say (boundary call
    9). `HomeService`'s own default is `ROW_PROVIDERS`.

    **And no embedder**, on the terms `get_taste_service` states: every
    similarity input this route reads is a precomputed artefact.

    **`refresh=queue.schedule` is what opens the grace window.** `HomeService`
    serves stale only when it has somewhere to hand the key, so this one
    argument is the difference between PRD 06's "served stale while
    refreshing" and "served stale". A bound method rather than a lambda so the
    thing being injected has a name, a docstring and a `__qualname__` a
    traceback can print -- and it is *synchronous*, which is the whole of "the
    screen never waits on it": there is nothing here for a handler to await.
    """
    return HomeService(cache=cache, refresh=refreshes.schedule)


HomeServiceDep = Annotated[HomeService, Depends(get_home_service)]
