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
from urllib.parse import quote

from cryptography.fernet import Fernet
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.lanes import LaneSupervisor
from usher.composition import adapter_factory, build_image_proxy_service, build_search_service
from usher.config import Settings
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.row_provider_settings import PostgresRowProviderSettingsRepository
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
from usher.ports.credentials import CredentialStore
from usher.ports.events import EventPublisher
from usher.ports.images import ImageBlobStore, ImageFetcher
from usher.ports.jobs import JobQueue
from usher.ports.repository import (
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    ImageRepository,
    MediaItemRepository,
    PersonRepository,
    RawPayloadStore,
    RowProviderSettingsRepository,
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
from usher.services.images import ImageProxyService
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.playback import PlaybackService
from usher.services.playback_ticket import build_ticket_cipher, mint
from usher.services.reconcile import ReconcileService
from usher.services.rows import enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RefreshQueue, RowCache
from usher.services.search import SearchService
from usher.services.similar import SimilarityService
from usher.services.sources import SourceService
from usher.services.taste import TasteService
from usher.services.titles import TitleReadService
from usher.services.watch_sync import WatchStateSyncService
from usher.services.watch_write import WatchWriteService


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


def get_credential_store(session: SessionDep, settings: SettingsDep) -> CredentialStore:
    """The encrypted credential store, on this request's session.

    Its own provider rather than being constructed inside
    `get_source_service`, for the reason `get_source_adapter_factory`'s
    docstring already gives about a second caller -- and `get_playback_service`
    is that second caller. Two sites each building their own would be two
    chances for one of them to drift onto a different session and quietly
    leave the request's transaction.

    The return type is the **port**, so a caller written against this
    annotation cannot reach a method `CredentialStore` does not have -- and
    `settings.secret_key` is handed over as the `SecretStr` it is, unwrapped
    only inside `PostgresCredentialStore`'s own key derivation.
    """
    return PostgresCredentialStore(session, settings.secret_key)


def get_source_service(
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    credentials: Annotated[CredentialStore, Depends(get_credential_store)],
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
    return SourceService(sources, credentials, adapters, lanes.push_available)


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
# The two `/play` routes resolve existence before resolving playability --
# `PlaybackService` reads `media_items`, which is silent about the difference
# between "no such title" and "no copy of it".
TitleRepositoryDep = Annotated[TitleRepository, Depends(get_title_repository)]
EpisodeRepositoryDep = Annotated[EpisodeRepository, Depends(get_episode_repository)]


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


# Declared here rather than beside the other M7 repositories below, because
# this is now its first user -- `Depends(...)` is evaluated when the `def`
# under it executes, so a provider appended after its consumer is a
# `NameError` at import of this module. `get_row_context` is its second.
def get_credit_repository(session: SessionDep) -> CreditRepository:
    return PostgresCreditRepository(session)


# Declared here rather than in the M7 block below, because this is its first
# user in the request graph -- `Depends(...)` is evaluated when the `def` under
# it executes, so a provider appended after its consumer is a `NameError` at
# import of this module. `get_row_context` is its second: C6's shelf artwork
# and C7's `images` key read the same port, and this is the one provider.
def get_image_repository(session: SessionDep) -> ImageRepository:
    """Artwork references for the request that is rendering them.

    The read half only, on `get_curated_row_repository`'s terms and for the
    same reason: `replace_for_titles` is a *derivation* -- a scoped delete plus
    an upsert over a title's whole artwork set -- and it belongs to
    `usher derive` under `JobKind.DERIVE`. The port is handed over whole
    because splitting a repository in two to express which half a caller uses
    is a second port for one table; what keeps the write off this path is that
    the two callers are `BaseRow.hydrate`, which calls `primary_for_titles`,
    and `TitleReadService.detail`, which calls `list_for_title`.
    """
    return PostgresImageRepository(session)


def get_title_read_service(
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
    media_items: MediaItemRepositoryDep,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    queue: JobQueueDep,
    credits: Annotated[CreditRepository, Depends(get_credit_repository)],
    images: Annotated[ImageRepository, Depends(get_image_repository)],
) -> TitleReadService:
    """Six repositories and the queue, and deliberately no adapter factory.

    The absence is the design (PRD 08: "a degraded subsystem narrows
    functionality; it never fails a request local state can answer"), not an
    omission that a later route should fill in: with no `SourceAdapter` in the
    graph there is no path from an unreachable Emby to a failed title read,
    and therefore no 503 for M5 to invent an error `code` for.
    `tests/unit/test_services_titles.py` asserts it on the service's own
    imports so that adding one here would fail rather than pass review.

    **`CreditRepository` and `ImageRepository` are the fifth and sixth and
    neither weakens that.** Both read tables `usher derive` fills from
    `raw_payloads` with no second network call, so neither adds a way for this
    route to depend on anything being up. It was four repositories until M9's
    `credits` key and five until its `images` key.

    ⚠️ **`ImageRepository` in particular is not the image proxy.**
    `GET /images/{id}` fetches bytes from a CDN and can fail because that CDN
    is down; this route reads *rows*, which is why an unreachable CDN narrows
    a client's screen to a missing picture and cannot touch this response's
    status code. The two are a separate route with a separate failure mode by
    construction, not by a caught exception -- `usher.ports.images` is not in
    this function's graph at all.
    """
    return TitleReadService(titles, media_items, sources, watch_states, queue, credits, images)


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


def get_collection_repository(session: SessionDep) -> CollectionRepository:
    return PostgresCollectionRepository(session)


# M9's `GET /people/{id}` reads the first two directly rather than through a
# service (`api/routers/people.py` says why), so the two repositories that were
# `RowContext` fields only now have route-facing annotations as well. Declared
# here beside their providers rather than at the bottom of the module: the
# aliases are what a router imports, and a reader following `PersonRepositoryDep`
# lands on the function that builds it.
PersonRepositoryDep = Annotated[PersonRepository, Depends(get_person_repository)]
CreditRepositoryDep = Annotated[CreditRepository, Depends(get_credit_repository)]
# And `GET /collections/{id}`, on the same terms: one port read plus a
# `TitleRepository.list_by_ids` hydration, with no service between them.
CollectionRepositoryDep = Annotated[CollectionRepository, Depends(get_collection_repository)]


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


def get_row_provider_settings_repository(session: SessionDep) -> RowProviderSettingsRepository:
    """The overrides table `GET`/`PUT /admin/rows/providers` renders and writes,
    and that `get_home_service` below filters the registry against.

    Request-scoped like every other repository here, and **not** cached on
    `app.state`: the whole point of the toggle is that the next request sees
    the stored value, and a process-lifetime read would make it a restart.
    """
    return PostgresRowProviderSettingsRepository(session)


RowProviderSettingsRepositoryDep = Annotated[
    RowProviderSettingsRepository, Depends(get_row_provider_settings_repository)
]


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
    the screen changes. `genre_affinity` is unaffected: it is counts over
    `titles.genres` and needs no model at all, which is the whole reason M7
    declined PRD 06's "taste centroid concentrated in a genre".

    ⚠️ **This docstring used to end "a deployment whose worker *did* compute a
    centroid cannot serve it from here". That is closed, and not here.**
    `centroid`'s contract is unchanged -- it still checks the embedder first,
    still refuses without one, and still writes its refusals -- because the
    thing a request needs is not a *computation* under a model it does not
    have. It is a **read**: `TasteRepository.latest(user_id)` answers the
    stored row whatever model wrote it, and `SearchService` uses it for PRD
    05's taste-centroid ranking term (`composition.build_search_service` wires
    it). So the gap is closed by a second port method rather than by giving
    this dependency an embedder, and `RowContext.taste` staying `None` is now a
    statement about the *row providers*, which read no centroid, rather than
    about what a request can reach. A provider that wanted one would take
    `latest` too.
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
    images: Annotated[ImageRepository, Depends(get_image_repository)],
    taste: Annotated[TasteService, Depends(get_taste_service)],
) -> RowContext:
    """The thirteen values a row may reach, for one request, for one user.

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

    **`images` is M9's, and it is the one field here whose reader is
    `BaseRow.hydrate` rather than a named provider.** A card's artwork is
    chosen against the *row's* `display_hint`, so the poster/backdrop decision
    belongs to the shelf and the read is one statement per shelf
    (`ImageRepository.primary_for_titles` takes a sequence precisely so the
    per-card shape cannot be expressed). It is the read half only, on
    `curated`'s terms: `replace_for_titles` is `usher derive`'s, and nothing on
    this path writes artwork.

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
        images=images,
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


RowCacheDep = Annotated[RowCache, Depends(get_row_cache)]


async def get_home_service(
    cache: RowCacheDep,
    refreshes: Annotated[RefreshQueue, Depends(get_refresh_queue)],
    provider_settings: RowProviderSettingsRepositoryDep,
) -> HomeService:
    """The composer, over the registry `services/rows/__init__.py` owns, minus
    what an operator has switched off.

    **The provider list is still not *assembled* here, and the distinction is
    the one M9's E2 must not blur.** Boundary call 9's argument -- *"a list a
    composition root builds by hand is a list the tenth provider is forgotten
    from"* -- is against **enumeration**, and this root names no provider: it
    hands `enabled_row_providers` the whole registry and removes the ones a
    stored row disables. An eleventh provider composes here with no edit,
    which is the property that argument protects.

    **The read is unconditional and precedes the screen-cache check, which is
    a cost stated rather than discovered.** `get_home_service` is a FastAPI
    dependency, so it resolves before the handler runs -- the same shape
    `RowContext.affinities` was made lazy to escape (`.claude/rules/
    rows-and-genome.md`: a 30 s cache hit was paying `list_recent(50)` plus a
    library-wide genre aggregate over 1.27M titles). It is left eager here
    because the two are not comparable: this is one `SELECT slug_prefix,
    enabled FROM row_provider_settings` over a table the registry bounds at
    **ten rows**, usually zero. Deferring it would mean a `HomeService` that
    took its providers as a callable -- a lazy field on the composer, for a
    read a sequential scan of ten rows answers -- and the cache hit it would
    save is a hit the toggle has already invalidated.

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
    return HomeService(
        enabled_row_providers(row_provider_settings(await provider_settings.overrides())),
        cache=cache,
        refresh=refreshes.schedule,
    )


HomeServiceDep = Annotated[HomeService, Depends(get_home_service)]


# ---------------------------------------------------------------------------
# Similarity (M9). `GET /titles/{id}/similar` is a thin read over
# `SimilarityService` and the `title_neighbors` artefact M6 built and shipped
# no route for (boundary call 1).
# ---------------------------------------------------------------------------


def get_similarity_service(
    session: SessionDep,
    embeddings: Annotated[TitleEmbeddingRepository, Depends(get_title_embedding_repository)],
    neighbors: Annotated[TitleNeighborRepository, Depends(get_title_neighbor_repository)],
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
) -> SimilarityService:
    """`commit` is `session.commit`, the same callable `get_session` calls at
    the end of a successful request -- and `SimilarityService.rebuild` is the
    only method that ever calls it. **The route built over this provider only
    reads** (`neighbors_of`, `computed_at`, `stale_neighbors`), so nothing on
    this path commits; the wiring exists because the service's fourth
    constructor argument is not optional, not because a write is reachable
    here. `usher similar --rebuild` is `rebuild`'s only caller, and nothing
    schedules it -- it is an operator's command or a cron entry.
    """
    return SimilarityService(embeddings, neighbors, titles, session.commit)


SimilarityServiceDep = Annotated[SimilarityService, Depends(get_similarity_service)]


# ---------------------------------------------------------------------------
# Playback (M9). `POST /titles/{id}/play`, `POST /episodes/{id}/play` and
# `GET /stream/{ticket}` -- the first routes in this API that hold a
# `SourceAdapter`, and therefore the first that can answer 503.
# ---------------------------------------------------------------------------


def get_ticket_cipher(settings: SettingsDep) -> Fernet:
    """This deployment's playback-ticket cipher.

    Its own provider so that the two sides of the ticket -- the mint below and
    `GET /stream/{ticket}`'s redeem -- derive their key through **one** call,
    and so that `settings.secret_key` is unwrapped in exactly one place on
    this path (inside `build_ticket_cipher`, which never binds the plaintext
    to a name).

    Per request rather than per process, deliberately. `build_ticket_cipher`
    is one HKDF-SHA256 expansion over a 32-byte input -- a single HMAC -- and
    caching it on `app.state` would mean an app that keeps minting valid
    tickets under a key the running `Settings` no longer names.
    """
    return build_ticket_cipher(settings.secret_key)


TicketCipherDep = Annotated[Fernet, Depends(get_ticket_cipher)]


def get_playback_service(
    request: Request,
    cipher: TicketCipherDep,
    media_items: MediaItemRepositoryDep,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    credentials: Annotated[CredentialStore, Depends(get_credential_store)],
    adapters: Annotated[SourceAdapterFactory, Depends(get_source_adapter_factory)],
) -> PlaybackService:
    """`PlaybackService`, with the mint closure this request's URL implies.

    **The mint returns a whole URL, not a token, and that is the seam
    `services/playback.py` was shaped for.** Its `mint` is
    `Callable[[str], str]` whose answer is substituted verbatim -- so a deep
    link wraps whatever comes back, and the service needs to know nothing
    about ciphers, TTLs or the redeem route's path.

    **`quote(ticket, safe="=")`, and the `safe` is measured rather than
    idiomatic.** A Fernet token's alphabet is url-safe base64 *plus* the `=`
    padding, and `=` is an RFC 3986 sub-delim and hence a legal `pchar`. D1
    measured that `quote(ticket, safe="")` -- the reflexive spelling -- is a
    no-op for only 192 of the 599 plaintext lengths 1-599, because it
    re-encodes `=` to `%3D`; `safe="="` is a no-op at every length tested.
    Starlette's own `url_path_for` substitutes the value raw, so if this line
    does not encode, nothing does.

    **`request.url_for`, not a hand-built string**, so the path can only ever
    be the redeem route's real path. ⚠️ It builds an absolute URL from the
    request's own `Host`, so **behind a reverse proxy that does not send
    `X-Forwarded-Proto`/`-Host` the ticket URL names the internal address.**
    That is an operator setting (`uvicorn --proxy-headers`, or
    `ProxyHeadersMiddleware`) rather than a code fix here -- naming it because
    the failure is a client following a URL it cannot reach, which looks like
    a playback bug.
    """

    def mint_ticket_url(url: str) -> str:
        ticket = mint(cipher, url, minted_at=datetime.now(UTC))
        return str(request.url_for("redeem_playback_ticket", ticket=quote(ticket, safe="=")))

    return PlaybackService(media_items, sources, credentials, adapters, mint_ticket_url)


PlaybackServiceDep = Annotated[PlaybackService, Depends(get_playback_service)]


# ---------------------------------------------------------------------------
# Search (M9). `GET /search` -- the first route over the retrieval M6 built and
# delivered through `usher search` alone.
# ---------------------------------------------------------------------------


def get_search_service(session: SessionDep, settings: SettingsDep) -> SearchService:
    """PRD 05's read path, request-scoped.

    **Reached through `usher.composition` rather than assembled here**, and
    that is a contract rather than a preference: contract 7 ("no concrete
    search, embedding or LLM implementation escapes its package") lists
    `usher.api` whole among its sources, so this module may not name
    `PostgresSearchIndex` even though it is a composition root and names
    `Postgres*` repositories on every other line. `allow_indirect_imports =
    true` is what sanctions the chain `usher.api.deps -> usher.composition ->
    usher.adapters.search.postgres` while leaving a direct import BROKEN.

    **Deliberately `build_search_service` and not `build_pipeline`.** The
    latter constructs the whole ingest graph -- matcher, reconciler,
    watch-state sync, similarity, ten row providers, the curation pool -- to
    reach one of its fields, once per request.

    **No embedder, and it is the same call `get_taste_service` and
    `get_home_service` make.** `create_app`'s lifespan builds a model only
    when `worker_enabled` and does not put it on `app.state`, so a dependency
    reaching for one would work in development and 500 in exactly the
    push-only deployment PRD 08 describes; it is also a once-per-process 65 MB
    resource this module already argues about for the TMDb token bucket.

    What that costs is visible on the wire rather than hidden, which is the
    difference from the two routes above: `?mode=semantic` answers a problem
    document naming the missing capability, and `?mode=fused` is served as
    full text with `requested_mode` and `mode` disagreeing so a client can say
    so. Closing it is a new capability (expose the lifespan's model, or build
    one per API process) rather than a change to this wiring.

    **And no expander**, on stricter terms than the embedder: an expansion is
    a paid completion in front of an embed, and with no embedder there is no
    embed for one to sit in front of. `SearchService` buys a completion only
    inside the `else` of its `embedder is None` branch, so this is not a
    saving that has to be argued -- there is no reachable call site.

    **The household is not wired here, and looking for it here is the mistake
    this paragraph exists to prevent.** `SearchService.search` takes a
    `user_id` per call, so the route reads `DefaultUserIdDep` beside this
    dependency and passes it in; what `build_search_service` wires is the
    `WatchStateRepository` the term reads *through*. A service built around one
    household would be a per-request object cached per session, and the two
    would disagree the first time a request carried an identity.

    **The taste term is served here despite the missing model, and that is a
    read rather than an exception to the paragraph above.** PRD 05's sixth
    ranking term needs a *centroid*, not an *embedder*:
    `build_search_service` wires a `TasteRepository` and a
    `TitleEmbeddingRepository`, and `SearchService` reads the household's
    stored row through `latest` and scopes its vector read by the model that
    row names. So a deployment whose worker computed a centroid serves it from
    this route with no model in this process -- which is the gap
    `get_taste_service` above spent a milestone describing.
    """
    return build_search_service(session, settings)


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
# The watch-write actions (M9). `PUT /watch/titles/{id}`,
# `PUT /watch/episodes/{id}` and the two `/played` routes -- the first routes
# in this API that write a `watch_states` row, and therefore the first writer
# of `origin = api`.
# ---------------------------------------------------------------------------


def get_watch_write_service(
    session: SessionDep,
    watch_states: Annotated[WatchStateRepository, Depends(get_watch_state_repository)],
    media_items: MediaItemRepositoryDep,
    queue: JobQueueDep,
    events: EventPublisherDep,
    cache: Annotated[RowCache, Depends(get_row_cache)],
) -> WatchWriteService:
    """`WatchWriteService`, holding no source adapter and no factory.

    **`commit` is `session.commit`, and unlike `get_reconcile_service`'s it is
    the whole point rather than a shared-wiring accident.** ADR-0033: an event
    is a statement about *committed* state. This service commits its own write
    before it publishes, so a subscriber told a position landed and refetching
    through a second connection finds it -- which is exactly what a route that
    left the commit to `get_session` could not promise. `get_session` still
    commits when the handler returns, and that second commit is what carries
    the enqueued write-back job.

    **The cache is the app's one `RowCache`, never a request-scoped one.** A
    request-scoped cache caches nothing, and an invalidation against one would
    drop entries nobody could ever have read -- leaving the household's real
    screen warm and stale, which is the subtle half of the bug
    `RowCache.invalidate` documents.
    """
    return WatchWriteService(
        watch_states=watch_states,
        media_items=media_items,
        queue=queue,
        events=events,
        commit=session.commit,
        cache=cache,
    )


WatchWriteServiceDep = Annotated[WatchWriteService, Depends(get_watch_write_service)]


# The image proxy (M9). One dependency function, deliberately: `app.py` and
# this module are the milestone's worst collision pair, and every other
# consumer of `images` -- `RowCard.artwork`, `GET /titles/{id}`'s `images` key
# -- reads the repository rather than this service, so a shared
# `get_image_repository` would be a second claimant on one line for no caller.
# ---------------------------------------------------------------------------


def get_image_proxy_service(request: Request, session: SessionDep) -> ImageProxyService:
    """`GET /images/{id}`'s service: this request's repository over the
    process's fetcher and store.

    **The asymmetry is the design, not an inconsistency.** The repository is
    session-scoped because a row read belongs to the request's unit of work;
    the fetcher and the store are process-scoped because the fetcher owns an
    `httpx.AsyncClient` and a client per request is a connection pool per
    request. `composition.image_proxy` builds both halves together in
    `create_app`'s lifespan, so a deployment cannot end up with a cache
    directory the fetcher's byte ceiling was never told about.

    **The repository, not the `Pipeline`.** `composition.build_image_proxy_
    service` says why: this route reads one row and needs none of the other
    twenty-odd fields, and a route handed the whole pipeline could reach the
    job queue from a request path.

    Same defensive `getattr`/`cast` shape as `get_session_factory`, and for
    the same reason -- `app.state` is typed `Any`.
    """
    fetcher = getattr(request.app.state, "image_fetcher", None)
    store = getattr(request.app.state, "image_store", None)
    if fetcher is None or store is None:
        raise RuntimeError(
            "app.state.image_fetcher/image_store is not set -- create_app's lifespan "
            "has not run. If this is a test using a bare ASGI transport, wrap the app "
            "in asgi_lifespan.LifespanManager first."
        )
    return build_image_proxy_service(
        PostgresImageRepository(session),
        cast(ImageFetcher, fetcher),
        cast(ImageBlobStore, store),
    )


ImageProxyServiceDep = Annotated[ImageProxyService, Depends(get_image_proxy_service)]
