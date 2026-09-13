"""Request-scoped dependencies, and the API's composition root."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, cast
from urllib.parse import quote

from cryptography.fernet import Fernet
from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.lanes import LaneSupervisor
from usher.composition import (
    SourceGateRegistry,
    adapter_factory,
    build_image_proxy_service,
    build_search_service,
    nothing,
)
from usher.config import Settings
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.row_provider_settings import PostgresRowProviderSettingsRepository
from usher.db.repositories.search import (
    PostgresTitleEmbeddingRepository,
    PostgresTitleNeighborRepository,
)
from usher.db.repositories.search_query import PostgresSearchQueryRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresRawPayloadStore, PostgresSyncRunRepository
from usher.db.repositories.taste import PostgresTasteRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import default_user, ensure_default_user
from usher.domain.taste import GenreAffinity
from usher.domain.watch import User
from usher.ports.credentials import CredentialStore
from usher.ports.embedding import Embedder
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
    SearchQueryRepository,
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
from usher.services.bootstrap import BootstrapReport, bootstrap_report
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
from usher.services.search import SearchQueryBuffer, SearchService
from usher.services.similar import SimilarityService
from usher.services.sources import SourceService
from usher.services.taste import TasteService
from usher.services.titles import TitleReadService
from usher.services.visibility import VisibilityService
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
    """Typed accessor for the session factory `create_app`'s lifespan installs on `app.state`.

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
    """Request-scoped session and the request's unit-of-work boundary.

    commits once the handler completes without raising, rolls back and re-raises
    otherwise.

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
    """The singleton `is_default` user's id, creating the row on first use."""
    return await ensure_default_user(session)


DefaultUserIdDep = Annotated[uuid.UUID, Depends(get_default_user_id)]


#: The household read, deferred. `usher.api.routers.search` is the caller.
Household = Callable[[], Awaitable[uuid.UUID]]


def get_household(session: SessionDep) -> Household:
    """`get_default_user_id`, handed over as the read rather than the answer.

    A dependency resolves on every request that declares it, including the ones
    that turn out not to need an id -- and `GET /search/suggest` declares one
    for a `search_queries` row it may not write, on a route a browser drives
    per keystroke. A route that takes this pays the `SELECT` only where the row
    is.
    """

    async def resolve() -> uuid.UUID:
        return await get_default_user_id(session)

    return resolve


HouseholdDep = Annotated[Household, Depends(get_household)]


def get_source_repository(session: SessionDep) -> SourceRepository:
    """Its own provider rather than being constructed inside `get_source_service`.

    because `get_title_read_service` needs the same one.

    Two callers each building their own would be two chances for one of them
    to drift onto a different session and quietly leave the request's
    transaction -- the failure `tests/integration/test_pipeline_deps.py`
    exists to make observable. Declared here, above its first user, because
    `Depends(...)` is evaluated when the `def` below executes.
    """
    return PostgresSourceRepository(session)


# `POST /admin/sources/{id}/sync` (M9's E3) reads through this and never
# through `SourceServiceDep` below -- `SourceService.status` builds an
# adapter and calls `verify()`, and a lookup that dials the upstream is not
# a lookup a 404 refusal should be paying for.
SourceRepositoryDep = Annotated[SourceRepository, Depends(get_source_repository)]


def get_source_gates(request: Request) -> SourceGateRegistry:
    """The process's outbound rate gates, off `app.state`.

    🔴 **The one thing on this page that must not be request-scoped, and the
    argument is already written forty lines below about `EnrichService`:** *"a
    request-scoped `TmdbClient` gives every concurrent request a fresh bucket,
    so N in-flight requests get N x 30 rps"*. A source gate built per request
    is that defect against a household's media server rather than against a
    CDN-backed public API -- and the server this project measures answers a
    single-item read in 0.1495 s, so N concurrent requests really do reach the
    rate the gate is set to refuse (`.claude/rules/emby-push-and-ingest.md`,
    2026-08-15, one household one evening). It is on `app.state` for the reason
    the image fetcher and the TMDb provider are, and it is the *same* object
    the two lanes read through, so an admin status probe and a push lane pace
    against one gate (ADR-0043 §4).

    Same defensive `getattr`/`cast` shape as `get_lane_supervisor` above and
    for the same reason -- `app.state` is typed `Any`.
    """
    gates = getattr(request.app.state, "source_gates", None)
    if gates is None:
        raise RuntimeError(
            "app.state.source_gates is not set -- create_app's lifespan has not run. "
            "If this is a test using a bare ASGI transport, wrap the app in "
            "asgi_lifespan.LifespanManager first."
        )
    return cast(SourceGateRegistry, gates)


def get_source_adapter_factory(
    settings: SettingsDep,
    gates: Annotated[SourceGateRegistry, Depends(get_source_gates)],
) -> SourceAdapterFactory:
    """The composition root's adapter registry.

    Its own dependency, not inlined into `get_source_service`, so a test can
    override exactly this one thing -- pointing the real `EmbyAdapter` at an
    in-memory server -- without also replacing the repository, the
    credential store, or the service.

    **Request-scoped, and that is now safe rather than merely tolerated.**
    The factory itself is cheap and stateless; the one piece of it whose
    lifetime matters is the gate registry, and that comes off `app.state`
    above rather than being minted here.
    """
    return adapter_factory(settings, gates)


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


# --------------------------------------------------------------------------- The ingest
# pipeline (M4).


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


async def get_bootstrap_report(session: SessionDep) -> BootstrapReport:
    """The report `usher bootstrap-status` prints, for the route to serialise.

    Assembled here rather than in the router for a structural reason rather
    than a stylistic one: `tests/unit/test_api_bootstrap.py` asserts that
    `api/routers/bootstrap.py` names no `usher.services.bootstrap` and no
    `usher.composition`, because the module holding a *trigger* for a
    multi-minute download must not be able to spell one. Building the value
    here and handing it over as `BootstrapReportDep` leaves the router with a
    dependency alias and a DTO, and leaves this module — the API's composition
    root, which already reaches `usher.db` on purpose — holding the three
    repositories.

    ⚠️ Two aggregate reads, ~0.33 s on a real 1.27M-title catalog;
    `BootstrapReport`'s docstring carries the measurement and the reason there
    is no cache. An admin screen, never a client path.
    """
    return await bootstrap_report(
        PostgresImportRunRepository(session),
        PostgresBulkCatalogRepository(session),
        PostgresGenomeRepository(session),
    )


MediaItemRepositoryDep = Annotated[MediaItemRepository, Depends(get_media_item_repository)]
SyncRunRepositoryDep = Annotated[SyncRunRepository, Depends(get_sync_run_repository)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
BootstrapReportDep = Annotated[BootstrapReport, Depends(get_bootstrap_report)]
# The two `/play` routes resolve existence before resolving playability --
# `PlaybackService` reads `media_items`, which is silent about the difference
# between "no such title" and "no copy of it".
TitleRepositoryDep = Annotated[TitleRepository, Depends(get_title_repository)]
EpisodeRepositoryDep = Annotated[EpisodeRepository, Depends(get_episode_repository)]


def get_visibility_service(
    queue: JobQueueDep,
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
) -> VisibilityService:
    """The demand lane for the screens (issue #73).

    Request-scoped and holding only the queue, which is what makes it unlike
    `EnrichService` — the note above explains why *that* one may not be a
    dependency, and the distinction is the token bucket rather than the shape.
    This promotes a job and makes no network call, so a per-request instance
    costs an object; enrichment itself still happens on the worker, which is
    the one process that owns the bucket.
    """
    return VisibilityService(queue, titles)


VisibilityServiceDep = Annotated[VisibilityService, Depends(get_visibility_service)]


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
    """`commit` is `session.commit`.

    the same callable `get_session` calls at the end of a successful request.

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


# `EnrichService` is deliberately absent, and this is the one place the plan was wrong
# rather than incomplete.
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
    """Six repositories and the queue, and deliberately no adapter factory."""
    return TitleReadService(titles, media_items, sources, watch_states, queue, credits, images)


TitleReadServiceDep = Annotated[TitleReadService, Depends(get_title_read_service)]


# --------------------------------------------------------------------------- The
# composed home screen (M7).


def get_title_neighbor_repository(session: SessionDep) -> TitleNeighborRepository:
    return PostgresTitleNeighborRepository(session)


def get_title_embedding_repository(session: SessionDep) -> TitleEmbeddingRepository:
    return PostgresTitleEmbeddingRepository(session)


def get_person_repository(session: SessionDep) -> PersonRepository:
    return PostgresPersonRepository(session)


def get_collection_repository(session: SessionDep) -> CollectionRepository:
    return PostgresCollectionRepository(session)


# M9's `GET /people/{id}` reads the first two directly rather than through a service
# (`api/routers/people.py` says why), so the two repositories that were `RowContext`
# fields only now have route-facing annotations as well.
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
    """The overrides table `GET`/`PUT /admin/rows/providers` renders and writes.

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
    """**No embedder, and that is the same call `get_home_service` makes.**."""
    return TasteService(
        watch_states=watch_states,
        embeddings=embeddings,
        titles=titles,
        taste=taste,
        embedder=None,
        now=lambda: datetime.now(UTC),
    )


class _Affinities:
    """This household's genre affinities, read on demand and then remembered."""

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
    """The thirteen values a row may reach, for one request, for one user."""
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
    visibility: VisibilityServiceDep,
) -> HomeService:
    """The composer.

    over the registry `services/rows/__init__.py` owns, minus what an operator has
    switched off.
    """
    return HomeService(
        enabled_row_providers(row_provider_settings(await provider_settings.overrides())),
        cache=cache,
        refresh=refreshes.schedule,
        visibility=visibility,
    )


HomeServiceDep = Annotated[HomeService, Depends(get_home_service)]


# ---------------------------------------------------------------------------
# Similarity (M9). `GET /titles/{id}/similar` is a thin read over
# `SimilarityService` and the `title_neighbors` artefact M6 built and shipped
# no route for (boundary call 1).
# ---------------------------------------------------------------------------


def get_similarity_service(
    session: SessionDep,
    settings: SettingsDep,
    embeddings: Annotated[TitleEmbeddingRepository, Depends(get_title_embedding_repository)],
    neighbors: Annotated[TitleNeighborRepository, Depends(get_title_neighbor_repository)],
    titles: Annotated[TitleRepository, Depends(get_title_repository)],
) -> SimilarityService:
    """`commit` is `session.commit`.

    the same callable `get_session` calls at the end of a successful request -- and
    `SimilarityService.rebuild` is the only method that ever calls it.

    **The route built over this provider only reads** (`neighbors_of`, `computed_at`,
    `stale_neighbors`), so nothing on this path commits; the wiring exists because the
    service's fourth constructor argument is not optional, not because a write is
    reachable here. `usher similar --rebuild` is `rebuild`'s only caller, and nothing
    schedules it -- it is an operator's command or a cron entry.

    **`settings` is here for `embedding_model` and for nothing else**, and it
    is a *setting* rather than an `Embedder` on purpose. `stale_neighbors` is
    one of the three reads this route does make, and it cannot answer without
    knowing which checkpoint the stored scores were computed under -- while a
    request has no model and must never load one (`create_app`'s lifespan
    builds one only under `worker_enabled`). Taking the name keeps the read
    answerable on every deployment, including one with no embedding extra
    installed at all.
    """
    return SimilarityService(
        embeddings,
        neighbors,
        titles,
        session.commit,
        embedding_model=settings.embedding_model,
    )


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
    """`PlaybackService`, with the mint closure this request's URL implies."""

    def mint_ticket_url(url: str) -> str:
        ticket = mint(cipher, url, minted_at=datetime.now(UTC))
        return str(request.url_for("redeem_playback_ticket", ticket=quote(ticket, safe="=")))

    return PlaybackService(media_items, sources, credentials, adapters, mint_ticket_url)


PlaybackServiceDep = Annotated[PlaybackService, Depends(get_playback_service)]


# ---------------------------------------------------------------------------
# Search (M9). `GET /search` -- the first route over the retrieval M6 built and
# delivered through `usher search` alone.
# ---------------------------------------------------------------------------


def get_search_service(
    request: Request, session: SessionDep, settings: SettingsDep
) -> SearchService:
    """PRD 05's read path, request-scoped."""
    # Annotated rather than passed straight through: `app.state` is typed
    # `Any`, so without a name carrying the port type nothing downstream of
    # this line is checked at all.
    model: Embedder | None = request.app.state.embedder
    # `commit=nothing`: `get_session` commits when the handler returns, so a
    # row that committed itself would end this request's transaction and leave
    # the demand promotion after it in a second one -- two WAL flushes where
    # one will do. The buffer is the lifespan's, read the way the model above
    # is, so a keystroke's row is written by the process's own drain.
    buffer: SearchQueryBuffer = request.app.state.search_queries
    return build_search_service(session, settings, embedder=model, commit=nothing, buffer=buffer)


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]


def get_search_query_repository(session: SessionDep) -> SearchQueryRepository:
    """`search_queries`' outcome half, for the routes a client reports one to.

    Separate from `get_search_service` above, which owns the *retrieval* half
    and reaches the same table through a `SearchAnalytics` pair carrying its
    own `commit`. The three routes here need neither: they write inside a
    request `get_session` already commits, and they hold no `SearchService`
    at all -- `GET /titles/{id}` and the two `/play` routes have no query to
    run. A dependency of their own is what keeps them from acquiring a search
    service in order to reach one `UPDATE`.
    """
    return PostgresSearchQueryRepository(session)


SearchQueryRepositoryDep = Annotated[SearchQueryRepository, Depends(get_search_query_repository)]


def get_search_id(
    search_id: Annotated[
        str | None,
        Query(
            description=(
                "Opaque `search_id` from a `GET /search` response, attributing this request "
                "to the search it came from. Optional; a value that is not one is ignored."
            )
        ),
    ] = None,
) -> uuid.UUID | None:
    """The `?search_id=` a client attached, parsed, or `None`."""
    if search_id is None:
        return None
    try:
        return uuid.UUID(search_id)
    except ValueError:
        return None


SearchIdDep = Annotated[uuid.UUID | None, Depends(get_search_id)]
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


# The image proxy (M9).


def get_image_proxy_service(request: Request, session: SessionDep) -> ImageProxyService:
    """`GET /images/{id}`'s service.

    this request's repository over the process's fetcher and store.

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
