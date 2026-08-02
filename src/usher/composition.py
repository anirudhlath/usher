"""The wiring both composition roots share.

PRD 01 gives Usher two composition roots -- `usher.api` and `usher.cli` --
and they have always had to assemble the *same* graph: `ReconcileService`'s
`MediaItemRepository` and `WatchStateSyncService`'s are one table, and
`services/` may not import `db/`
([ADR-0009](../../docs/prd/decisions/0009-repositories-are-ports.md)), so
nothing below the root can assemble itself. Until M5 the CLI held the
only full assembly and `api/deps.py` held a request-scoped echo of it.

M5 gives the server process its own background lanes, which need the whole
graph rather than a request's slice of it. **A second copy is how two
composition roots drift**, and the drift is silent: one root gains an
`EventPublisher` and the other keeps publishing to nowhere, or one passes
this deployment's `push_stale_after_seconds` and the other takes the
adapter's default. So the assembly moved here and both roots call it.

**Not a third composition root.** Nothing here decides *when* to run
anything, opens a session, or owns a process lifetime; it takes a session
and returns objects. `usher.cli` owns one session per command,
`usher.api.lanes` owns one per unit of work, and both of those decisions
stay where they are.

**Why this is not in `usher.cli`.** That module carries an import-linter
contract saying nothing may import it, precisely because it *is* a
composition root. Shared code there would either break that contract or
force it to be weakened.

**Why no seventh import-linter contract.** `usher.composition` imports
`usher.db` and `usher.adapters`, so a core module reaching it breaks
contracts two and three -- which report indirect chains by default, unlike
contract six's `allow_indirect_imports = true`. Verified by planting
`from usher.composition import Pipeline` in `usher/services/push.py`: two
contracts break. So the hole an unlisted module would otherwise leave is
closed by what this module itself imports rather than by a rule.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.adapters.tmdb import TmdbClient, TmdbMetadataProvider
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
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.ports.credentials import CredentialStore
from usher.ports.events import EventPublisher, NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.metadata import MetadataProvider
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
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.enrich import EnrichService
from usher.services.handlers import (
    SourceBinding,
    enrich_handler,
    match_handler,
    watch_history_handler,
)
from usher.services.ingest import IngestService
from usher.services.jobs import JobWorker
from usher.services.matching import MatchService
from usher.services.push import PushApplyService
from usher.services.reconcile import ReconcileService
from usher.services.watch_sync import WatchStateSyncService
from usher.telemetry import QueueSnapshot

# What a caller is told when a source's credential row has gone missing.
# One string rather than one per root: `usher sync` prints it, the lane
# supervisor logs it, and an operator reading either should be reading the
# same sentence.
NO_CREDENTIALS = "no stored credentials; re-enter them to reconnect"

# One session, one pipeline, for the length of one unit of work. Spelled as
# a callable returning a context manager rather than as a session factory so
# that `usher.api.lanes` -- the only long-lived consumer -- depends on the
# *wiring* rather than on SQLAlchemy, and so a lane test can supply a
# pipeline over fakes with no database in it at all.
UnitOfWork = Callable[[], AbstractAsyncContextManager["Pipeline"]]


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Every service and repository the pipeline needs, on one session.

    This is what a composition root *is*: the one place allowed to know
    that `ReconcileService`'s `MediaItemRepository` and
    `WatchStateSyncService`'s are the same table.

    Held as **ports**, not as the `Postgres*` classes that fill them, so
    the assembly reads as the wiring diagram rather than as a second copy
    of the implementation list -- and so a caller cannot reach a method the
    port does not have. (`usher.cli._Pipeline` claimed exactly this in its
    docstring and annotated the concrete classes; the claim is true here.)

    `commit` is the session's own, carried alongside the repositories
    because every service that writes takes one and a caller that had to
    keep the session to hand would be holding two halves of one thing.
    """

    sources: SourceRepository
    credentials: CredentialStore
    titles: TitleRepository
    matching: TitleMatchRepository
    media_items: MediaItemRepository
    episodes: EpisodeRepository
    watch_states: WatchStateRepository
    payloads: RawPayloadStore
    runs: SyncRunRepository
    queue: JobQueue
    adapters: SourceAdapterFactory
    matcher: MatchService
    ingest: IngestService
    reconcile: ReconcileService
    watch: WatchStateSyncService
    events: EventPublisher
    commit: Callable[[], Awaitable[None]]


def adapter_factory(settings: Settings) -> SourceAdapterFactory:
    """This deployment's tuning, applied to every adapter it builds.

    One function rather than three constructions, because a knob added to
    the registry has to reach the server, the CLI and the lanes at once --
    `push_stale_after_seconds` reaching two of the three is a source whose
    staleness window depends on which process opened its socket.
    """
    return ConfiguredSourceAdapterFactory(
        page_size=settings.source_page_size,
        timeout_seconds=settings.source_timeout_seconds,
        reauth_cooldown_seconds=settings.source_reauth_cooldown_seconds,
        push_stale_after_seconds=settings.push_stale_after_seconds,
        push_poll_seconds=settings.push_poll_seconds,
    )


def build_pipeline(
    session: AsyncSession,
    settings: Settings,
    *,
    events: EventPublisher | None = None,
    max_retract_fraction: float | None = None,
    provider: MetadataProvider | None = None,
) -> Pipeline:
    """Wire one session into the whole ingest pipeline.

    `events` defaults to `NullEventPublisher()` and that default is the
    honest one for a *separate process*: `usher sync` and `usher work` have
    no SSE client on the other side of a publish, because M5's bus is
    in-process. The server's lanes pass the real bus, which is the whole
    reason PRD 03's read-through loop closes there and not here. Stated as
    a default rather than as a branch, so the seam a
    `PostgresNotifyEventBus` slots into is one argument rather than an `if`.

    `max_retract_fraction` overrides `settings.sync_max_retract_fraction`;
    `usher sync --allow-full-retraction` is the only caller that passes
    one, and it passes `1.0` (ADR-0015).

    `provider` is `None` for every path that runs *inside a walk*. That is
    not an omission: `MatchService`'s batch path must never make a network
    call per unmatched item, and its constructor takes the provider as
    optional for exactly that reason. Only a worker -- which runs the
    queued `match` and `enrich` handlers -- passes one.
    """
    publisher = NullEventPublisher() if events is None else events
    sources = PostgresSourceRepository(session)
    credentials = PostgresCredentialStore(session, settings.secret_key)
    titles = PostgresTitleRepository(session)
    matching = PostgresTitleMatchRepository(session)
    media_items = PostgresMediaItemRepository(session)
    episodes = PostgresEpisodeRepository(session)
    watch_states = PostgresWatchStateRepository(session)
    payloads = PostgresRawPayloadStore(session)
    runs = PostgresSyncRunRepository(session)
    queue = PostgresJobQueue(
        session,
        max_attempts=settings.job_max_attempts,
        backoff_seconds=settings.job_backoff_seconds,
    )
    matcher = MatchService(titles=titles, matching=matching, queue=queue, provider=provider)
    ingest = IngestService(
        matcher=matcher,
        matching=matching,
        media_items=media_items,
        episodes=episodes,
        queue=queue,
    )
    return Pipeline(
        sources=sources,
        credentials=credentials,
        titles=titles,
        matching=matching,
        media_items=media_items,
        episodes=episodes,
        watch_states=watch_states,
        payloads=payloads,
        runs=runs,
        queue=queue,
        adapters=adapter_factory(settings),
        matcher=matcher,
        ingest=ingest,
        reconcile=ReconcileService(
            ingest=ingest,
            media_items=media_items,
            runs=runs,
            events=publisher,
            commit=session.commit,
            batch_size=settings.sync_batch_size,
            max_retract_fraction=(
                settings.sync_max_retract_fraction
                if max_retract_fraction is None
                else max_retract_fraction
            ),
        ),
        watch=WatchStateSyncService(
            media_items=media_items,
            watch_states=watch_states,
            runs=runs,
            queue=queue,
            commit=session.commit,
            batch_size=settings.sync_batch_size,
        ),
        events=publisher,
        commit=session.commit,
    )


async def selected_sources(pipeline: Pipeline, name: str | None = None) -> list[Source]:
    """Every enabled source, or the one named.

    A disabled source is skipped even when named explicitly: `enabled` is
    how an operator parks a server that is being rebuilt, and honouring the
    name over the flag would walk it anyway. The lane supervisor relies on
    the same rule from the other side -- a source disabled at runtime loses
    its lane on the next refresh.
    """
    sources = [source for source in await pipeline.sources.list_all() if source.enabled]
    if name is None:
        return sources
    return [source for source in sources if source.name == name]


async def open_adapter(pipeline: Pipeline, source: Source) -> SourceAdapter | None:
    """Build the adapter for one source, or `None` if its credential row
    has gone missing.

    `None` rather than a raise: an operator with three sources needs the
    second and third to run when the first's credential has gone -- exactly
    the reasoning `ReconcileService.reconcile` applies one layer down to an
    unreachable server, and the reason a lane for a broken source does not
    stop the other lanes starting.
    """
    credentials = await pipeline.credentials.get(source.credentials_ref)
    if credentials is None:
        logger.warning("{source}: {reason}", source=source.name, reason=NO_CREDENTIALS)
        return None
    return pipeline.adapters.build(source, credentials)


def build_push_applier(
    pipeline: Pipeline, settings: Settings, events: EventPublisher
) -> PushApplyService:
    """One push event into catalog state, through M4's own chain.

    `events` is passed explicitly rather than taken off the pipeline
    because the applier is the one collaborator whose publisher *must* be
    the live bus -- a push merge nobody is told about is the read-through
    loop not closing, which is the milestone.
    """
    return PushApplyService(
        pipeline.ingest,
        pipeline.watch,
        events,
        pipeline.commit,
        max_items_per_event=settings.push_max_items_per_event,
    )


def build_enrich_service(
    pipeline: Pipeline, settings: Settings, provider: MetadataProvider
) -> EnrichService:
    return EnrichService(
        titles=pipeline.titles,
        episodes=pipeline.episodes,
        payloads=pipeline.payloads,
        provider=provider,
        commit=pipeline.commit,
        events=pipeline.events,
        cache_max_age_days=settings.enrich_cache_max_age_days,
    )


def build_worker(
    pipeline: Pipeline,
    settings: Settings,
    *,
    provider: MetadataProvider | None,
    resolve: Callable[[str], Awaitable[SourceBinding | None]],
    user_id: uuid.UUID,
) -> JobWorker:
    """The queue consumer, with a handler per `JobKind` this process can run.

    Shared because `usher work` and the server's worker lane must register
    the *same* handlers: a lane that quietly lacked `enrich` would leave a
    demand-promoted job at the head of the queue forever, and the client
    that promoted it watching an SSE stream that never fires.
    """
    worker = JobWorker(pipeline.queue, pipeline.commit, batch_size=settings.job_batch_size)
    worker.register(JobKind.MATCH, match_handler(pipeline.matcher, pipeline.media_items, resolve))
    worker.register(
        JobKind.WATCH_HISTORY, watch_history_handler(pipeline.watch, resolve, user_id=user_id)
    )
    if provider is not None:
        worker.register(
            JobKind.ENRICH, enrich_handler(build_enrich_service(pipeline, settings, provider))
        )
    else:
        # Not a silent skip: PRD 08's "TMDb key missing" degradation is a
        # *narrowed* deployment, and an operator whose enrich queue never
        # drains has to be able to see why.
        logger.warning("no TMDb API key configured; enrich jobs will not be claimed")
    return worker


async def metadata_provider(
    settings: Settings,
) -> tuple[MetadataProvider | None, Callable[[], Awaitable[None]]]:
    """The TMDb provider and the callable that closes its transport.

    Returns `(None, no-op)` when no key is configured, rather than raising:
    `match` and `watch_history` jobs need no provider at all, and a worker
    that refused to start without a TMDb key would take two working lanes
    down with the third.

    One client per *process*, not per job or per pass, because the token
    bucket that keeps this deployment under TMDb's ~40 rps ceiling lives on
    the client. A client per job would give every job its own budget, which
    is a rate limiter that limits nothing.
    """
    if settings.tmdb_api_key is None:

        async def _nothing() -> None:
            return None

        return None, _nothing
    client = httpx.AsyncClient(timeout=settings.source_timeout_seconds)
    provider = TmdbMetadataProvider(
        TmdbClient(
            client,
            settings.tmdb_api_key,
            base_url=settings.tmdb_base_url,
            requests_per_second=settings.tmdb_requests_per_second,
        ),
        region=settings.tmdb_region,
    )
    return provider, client.aclose


def unit_of_work(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    events: EventPublisher,
    provider: MetadataProvider | None = None,
) -> UnitOfWork:
    """One session, one pipeline, one transaction, closed however it ends.

    This is the shape a long-lived lane needs and a command does not: a
    supervisor that held a session would hold it for the life of a socket
    -- hours, idle in transaction, with a snapshot from whenever the lane
    started -- so every unit of work opens its own. Returned as a callable
    so `usher.api.lanes` never imports SQLAlchemy at all, and so a test can
    hand it a pipeline over fakes without standing up a database.
    """

    @asynccontextmanager
    async def open() -> AsyncIterator[Pipeline]:
        async with sessions() as session:
            yield build_pipeline(session, settings, events=events, provider=provider)

    return open


class DefaultUserId:
    """`ensure_default_user`, resolved once and then remembered.

    **Inside a lane's own unit of work, never at startup.**
    `usher.api.deps.get_default_user_id` states the argument for the
    request-scoped half and it applies here unchanged: `create_app`'s
    lifespan builds an engine and opens no connection, which is what makes
    `/health` answer 200 with Postgres down while `/health/ready` reports
    503. A write at startup would turn a database outage into a crash loop
    and an unmigrated schema into a failure to boot. A lane task's failures
    are caught, logged and retried, so the same call here delays the first
    job instead of failing the boot.

    Cached after the first success so a lane polling every few seconds does
    not re-read the row every pass; **not** cached on failure, so a lane
    that started before the database did still gets an answer.
    """

    __slots__ = ("_sessions", "_user_id")

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._user_id: uuid.UUID | None = None

    async def __call__(self) -> uuid.UUID:
        if self._user_id is None:
            async with self._sessions() as session:
                user_id = await ensure_default_user(session)
                await session.commit()
            self._user_id = user_id
        return self._user_id


class SourceRegistry:
    """`external_id` -> the configured source that addresses it.

    `Job.key` for `match` and `watch_history` is a source's own
    `external_id` (`usher.domain.jobs.Job` says why: turning it into a
    `MediaItem.id` at enqueue time would cost a round trip per item, 1.1M a
    walk). So a handler has to find *which* source that string belongs to,
    and a household with two servers means a worker bound to one of them
    silently drops the other's jobs.

    Adapters are built lazily and cached for the life of the registry: one
    adapter is one connection pool, and building one per job would
    re-authenticate against the upstream every time.

    **The pipeline is rebindable and the adapter cache is not.** A worker
    lane inside the server opens a fresh session per pass, so its
    repositories change every few seconds while its connection pools must
    not -- `rebind` is that split made explicit rather than a registry
    holding a session that has been closed under it. `usher work`, which
    holds one session for the whole command, never calls it.
    """

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline
        self._adapters: dict[uuid.UUID, SourceAdapter] = {}

    def rebind(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    async def resolve(self, external_id: str) -> SourceBinding | None:
        for source in await self._pipeline.sources.list_all():
            if not source.enabled:
                continue
            stored = await self._pipeline.media_items.get_by_external_id(source.id, external_id)
            if stored is None:
                continue
            adapter = self._adapters.get(source.id)
            if adapter is None:
                adapter = await open_adapter(self._pipeline, source)
                if adapter is None:
                    return None
                self._adapters[source.id] = adapter
            return SourceBinding(source=source, adapter=adapter)
        return None

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
        self._adapters.clear()


class QueueGauges:
    """The `jobs` table as PRD 10's two gauges see it.

    A held snapshot, refreshed after every pass, because an OTel observable
    callback runs on the reader's background thread and cannot await an
    asyncpg query -- `register_queue_gauges`' docstring has the whole
    argument. Refreshing after each pass rather than before it means the
    reported depth is the depth *left over*, which is the number "ingest
    stalled" (PRD 10's alert) is actually about.

    `refresh` takes the queue rather than holding one, because the worker
    lane's queue is bound to a session that lives for one pass while this
    snapshot outlives every pass.
    """

    __slots__ = ("_snapshot",)

    def __init__(self) -> None:
        self._snapshot = QueueSnapshot()

    def read(self) -> QueueSnapshot:
        return self._snapshot

    async def refresh(self, queue: JobQueue) -> None:
        depth = await queue.depth()
        parked = await queue.parked(limit=1000)
        counts = dict.fromkeys((kind.value for kind in JobKind), 0)
        for job in parked:
            counts[job.kind.value] += 1
        self._snapshot = QueueSnapshot(
            queued={kind.value: count for kind, count in depth.items()}, parked=counts
        )


__all__ = [
    "NO_CREDENTIALS",
    "DefaultUserId",
    "Pipeline",
    "QueueGauges",
    "SourceRegistry",
    "adapter_factory",
    "build_enrich_service",
    "build_pipeline",
    "build_push_applier",
    "build_worker",
    "metadata_provider",
    "open_adapter",
    "selected_sources",
    "unit_of_work",
]
