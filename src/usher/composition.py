"""The wiring both composition roots share."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.bulk.imdb import (
    IMDbAkaDataset,
    IMDbCreditNamesDataset,
    IMDbRatingDataset,
    IMDbTitleDataset,
)
from usher.adapters.bulk.movielens import GENOME_BATCH_SIZE, MovieLensGenomeDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.adapters.embedding.fastembed import RUNTIME as FASTEMBED_RUNTIME
from usher.adapters.embedding.openai_compat import RUNTIME as OPENAI_RUNTIME
from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.adapters.http import SourceGateRegistry
from usher.adapters.images import DiskImageBlobStore, ProviderCdnImageFetcher
from usher.adapters.llm import OpenAICompatibleClient
from usher.adapters.search.postgres import PostgresSearchIndex, PostgresSuggestIndex
from usher.adapters.search.prefix import PostgresPrefixSuggestIndex
from usher.adapters.tmdb import TmdbClient, TmdbMetadataProvider
from usher.config import Settings
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.llm_call import PostgresLLMCallRepository
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
from usher.db.users import ensure_default_user
from usher.domain.bootstrap import BootstrapPhase, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.watch import User
from usher.ports.bulk import GenomeVector, ImdbAka, ImdbCreditNames, ImdbTitle
from usher.ports.credentials import CredentialStore
from usher.ports.embedding import Embedder
from usher.ports.events import EventPublisher, NullEventPublisher
from usher.ports.images import ImageBlobStore, ImageFetcher
from usher.ports.jobs import JobQueue
from usher.ports.llm import LLMClient
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import (
    BulkCatalogRepository,
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    GenomeCoverage,
    ImageRepository,
    ImportRunRepository,
    LLMCallRepository,
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
from usher.ports.rows import RowContext, RowProvider
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.bootstrap import BootstrapService
from usher.services.curation import CurationService
from usher.services.curation_pool import CandidatePoolService
from usher.services.derive import DeriveService
from usher.services.enrich import EnrichService
from usher.services.events import DeferredEventPublisher
from usher.services.handlers import (
    SourceBinding,
    bootstrap_handler,
    curate_handler,
    derive_handler,
    enrich_handler,
    index_handler,
    match_handler,
    sync_handler,
    watch_history_handler,
    watch_writeback_handler,
)
from usher.services.images import ImageProxyService
from usher.services.index import IndexService
from usher.services.ingest import IngestService
from usher.services.jobs import (
    KIND_CONCURRENCY,
    Handler,
    JobScope,
    JobWorker,
)
from usher.services.matching import MatchService
from usher.services.push import PushApplyService
from usher.services.query_expansion import QueryExpansionService
from usher.services.reconcile import ReconcileService
from usher.services.rows import row_providers
from usher.services.rows.cache import RowCache
from usher.services.scheduler import (
    RETENTION_PERIOD,
    Scheduler,
    SearchQueryRetention,
    SearchQueryScope,
)
from usher.services.search import SearchAnalytics, SearchQueryBuffer, SearchService
from usher.services.similar import (
    NeighborRebuildJob,
    SimilarityScope,
    SimilarityService,
    blend_fingerprint,
)
from usher.services.taste import TasteService
from usher.services.watch_sync import WatchStateSyncService
from usher.telemetry import QueueSnapshot, SearchSnapshot

# What a caller is told when a source's credential row has gone missing.
# One string rather than one per root: `usher sync` prints it, the lane
# supervisor logs it, and an operator reading either should be reading the
# same sentence.
NO_CREDENTIALS = "no stored credentials; re-enter them to reconnect"


async def nothing() -> None:
    """The no-op half of every `(thing, close it)` pair in this module.

    Module scope rather than one closure per factory, so the degradation
    paths cannot drift into "one returns a callable and one returns None" --
    a caller must be able to `await aclose()` unconditionally whether or not
    the thing was built. Public because both composition roots need the same
    object for the lane they did not build: four functions that do nothing
    are still four things to keep the same, and the `finally` that awaits
    them has one shape rather than an `if`.
    """
    return None


# One session, one pipeline, for the length of one unit of work. Spelled as
# a callable returning a context manager rather than as a session factory so
# that `usher.api.lanes` -- the only long-lived consumer -- depends on the
# *wiring* rather than on SQLAlchemy, and so a lane test can supply a
# pipeline over fakes with no database in it at all.
UnitOfWork = Callable[[], AbstractAsyncContextManager["Pipeline"]]

# The engine-bound session factory, named so a long-lived consumer can hold one without
# naming SQLAlchemy.
SessionFactory = async_sessionmaker[AsyncSession]


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
    # M2's two bulk-import ports, on the pipeline for the reason every other
    # port here is: `run_bootstrap` is one dispatch two roots call, and
    # `build_worker` sees a `Pipeline` and nothing else.
    bulk: BulkCatalogRepository
    import_runs: ImportRunRepository
    queue: JobQueue
    embeddings: TitleEmbeddingRepository
    neighbors: TitleNeighborRepository
    taste_rows: TasteRepository
    # M8's table, and the field is here because `usher home` assembles a `RowContext`
    # from this pipeline exactly as `api/deps.py` assembles one from its request-scoped
    # dependencies -- `CuratedProvider` reads `list_for_user` through the context and a
    # CLI that could not fill that field would compose a screen the route does not.
    curated_rows: CuratedRowRepository
    # M8's cost ledger. Write-only from here -- nothing in `src/` reads it
    # back, and PRD 10's spend dashboards are SQL against the table -- so it
    # is on the pipeline for the reason every other port is: `services/` may
    # not import `db/`, and a `CurationService` handed a ledger of its own
    # would attribute a real charge to an object nobody reads.
    llm_calls: LLMCallRepository
    people: PersonRepository
    credits: CreditRepository
    collections: CollectionRepository
    # M9's table, and the only writer is `DeriveService` -- artwork is
    # re-derived from `raw_payloads` on the same walk as people and credits
    # (M4's boundary call 2), and the serve path reads it back through
    # `get`/`primary_for_titles`. One object per session, for the reason
    # every port on this dataclass is here: `services/` may not import `db/`.
    images: ImageRepository
    adapters: SourceAdapterFactory
    matcher: MatchService
    ingest: IngestService
    reconcile: ReconcileService
    watch: WatchStateSyncService
    search: SearchService
    similar: SimilarityService
    taste: TasteService
    # M8's candidate pool. A *service* rather than a port, unlike every field
    # above it except the other five services: it composes two repository
    # reads and `TasteService`, and `CurationService` is what will hold it.
    pool: CandidatePoolService
    # The registry itself, not a list assembled here. A provider enabled by
    # *registration in code* is boundary call 9, and a list a composition
    # root builds by hand is a list the tenth provider is forgotten from --
    # which is dead code that looks exactly like a provider with nothing to
    # say. `services/rows/__init__.py` owns it; this field is the wiring.
    row_providers: tuple[RowProvider, ...]
    # M9's overrides table, and the field is here because the registry above is only
    # half of "which providers compose".
    row_provider_settings: RowProviderSettingsRepository
    events: EventPublisher
    commit: Callable[[], Awaitable[None]]


def source_gates(settings: Settings) -> SourceGateRegistry:
    """This process's outbound rate gates, one per source.

    **Built once at a composition root and handed down**, which is why
    `adapter_factory` below takes it rather than reading the rate itself.
    `create_app`'s lifespan builds one and puts it on
    `app.state` so the two lanes and every request share it; `usher work` and
    `usher sync` each build one for the life of the command. `unit_of_work`
    builds one when nobody hands it one, so the default is *shared across every
    scope that unit of work opens* rather than fresh per scope.

    Precisely the shape `api/app.py` already uses for `TmdbClient`'s token
    bucket, and for the reason `api/deps.py` records beside `EnrichService`:
    a limiter whose lifetime is a request is a limiter multiplied by the
    number of requests in flight.

    **A second process is a second registry**, and that is a capacity decision
    rather than a correctness one -- two `usher work` containers against one
    Emby spend `2 x rate`. Nothing here reaches across a process boundary and
    nothing should pretend to.
    """
    return SourceGateRegistry(settings.source_requests_per_second)


def adapter_factory(settings: Settings, gates: SourceGateRegistry) -> SourceAdapterFactory:
    """This deployment's tuning, applied to every adapter it builds."""
    return ConfiguredSourceAdapterFactory(
        page_size=settings.source_page_size,
        timeout_seconds=settings.source_timeout_seconds,
        reauth_cooldown_seconds=settings.source_reauth_cooldown_seconds,
        gates=gates,
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
    embedder: Embedder | None = None,
    llm: LLMClient | None = None,
    gates: SourceGateRegistry | None = None,
) -> Pipeline:
    """Wire one session into the whole ingest pipeline."""
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
    embeddings = PostgresTitleEmbeddingRepository(session)
    neighbors = PostgresTitleNeighborRepository(session)
    taste_rows = PostgresTasteRepository(session)
    curated_rows = PostgresCuratedRowRepository(session)
    llm_calls = PostgresLLMCallRepository(session)
    queue = PostgresJobQueue(
        session,
        max_attempts=settings.job_max_attempts,
        backoff_seconds=settings.job_backoff_seconds,
    )
    people = PostgresPersonRepository(session)
    credits = PostgresCreditRepository(session)
    collections = PostgresCollectionRepository(session)
    images = PostgresImageRepository(session)
    matcher = MatchService(titles=titles, matching=matching, queue=queue, provider=provider)
    ingest = IngestService(
        matcher=matcher,
        matching=matching,
        media_items=media_items,
        episodes=episodes,
        queue=queue,
    )
    # **The embedder is passed here too and may be `None`, which is the shipped
    # default.** `CandidatePoolService` then gets `None` from `TasteService.centroid`
    # and returns the base order whole -- M8's boundary call 5, and the reason the pool
    # is built from signals that need no model.
    taste = TasteService(
        watch_states=watch_states,
        embeddings=embeddings,
        titles=titles,
        taste=taste_rows,
        embedder=embedder,
        now=lambda: datetime.now(UTC),
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
        bulk=PostgresBulkCatalogRepository(session),
        import_runs=PostgresImportRunRepository(session),
        runs=runs,
        queue=queue,
        embeddings=embeddings,
        neighbors=neighbors,
        taste_rows=taste_rows,
        curated_rows=curated_rows,
        llm_calls=llm_calls,
        people=people,
        credits=credits,
        collections=collections,
        images=images,
        # **The gate registry travels with the composition root, not with the
        # pipeline.** `None` means "nobody handed me one", which is one command's single
        # pipeline (`usher sync`) or a directly-built pipeline in a test -- there is
        # nothing for it to share a gate *with*, so a private registry is the honest
        # answer.
        adapters=adapter_factory(settings, gates if gates is not None else source_gates(settings)),
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
        # **Delegated to `build_search_service` rather than spelled here**, so this
        # deployment's search tuning has exactly one assembly.
        search=build_search_service(
            session,
            settings,
            embedder=embedder,
            expander=(
                None
                if llm is None or not settings.query_expansion_enabled
                else QueryExpansionService(
                    client=llm,
                    ledger=llm_calls,
                    commit=session.commit,
                    model=settings.llm_model,
                )
            ),
        ),
        # **No embedder here, in either form.** The rebuild reads stored
        # vectors and never embeds anything, which is why `usher similar`
        # starts in 0.13 s rather than paying a 4.84 s cold model load --
        # and why a deployment with no embedding extra can still read and
        # rebuild neighbours for whatever the worker did index.
        similar=SimilarityService(
            embeddings,
            neighbors,
            titles,
            session.commit,
            # The *name*, never an `Embedder`: this service reads stored
            # vectors and the name is what `blend_fingerprint` hashes.
            embedding_model=settings.embedding_model,
        ),
        # **The embedder is passed and may be `None`, which is the shipped
        # default.** `TasteService.centroid` then returns `None` rather than a
        # zero vector and every consumer drops the signal; `genre_affinity` is
        # unaffected because it reads counts rather than vectors.
        row_providers=row_providers(semantic=embedder is not None),
        row_provider_settings=PostgresRowProviderSettingsRepository(session),
        taste=taste,
        # The pool is the whole of M8's retrieval half, and its size is the
        # prompt's token budget: **~20.4 prompt tokens a candidate**, flat from
        # a pool of 8 to a pool of 600.
        pool=CandidatePoolService(
            titles=titles,
            embeddings=embeddings,
            taste=taste,
            size=settings.curation_pool_size,
        ),
        events=publisher,
        commit=session.commit,
    )


def build_search_service(
    session: AsyncSession,
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    expander: QueryExpansionService | None = None,
    commit: Callable[[], Awaitable[None]] | None = None,
    buffer: SearchQueryBuffer | None = None,
) -> SearchService:
    """PRD 05's read path on one session, and nothing else."""
    return SearchService(
        PostgresSearchIndex(
            session,
            ef_search=settings.search_hnsw_ef_search,
            rrf_k=settings.search_rrf_k,
        ),
        # Tier 1 first, matching `SuggestTier`'s own order and the route's.
        # Two adjacent arguments of one type, so the names on the other side
        # are what stop a swap -- swapped, the keystroke tier becomes the
        # 33.6 ms one and both still answer.
        PostgresPrefixSuggestIndex(session),
        PostgresSuggestIndex(
            session,
            threshold=settings.search_trigram_threshold,
            candidates=settings.search_suggest_candidates,
        ),
        PostgresTitleRepository(session),
        PostgresMediaItemRepository(session),
        # The fifth object, and it is built here rather than handed in for the
        # reason the two indexes are: it is a function of the session alone.
        # **Built here rather than in `build_pipeline` and again in
        # `api/deps.py`** -- this function is the one assembly, so a caller
        # that reaches it gets the watch-state term or nobody does.
        PostgresWatchStateRepository(session),
        # Six and seven, on the same terms, and they are what makes the taste term
        # reachable from a request.
        PostgresTasteRepository(session),
        PostgresTitleEmbeddingRepository(session),
        result_limit=settings.search_result_limit,
        embedder=embedder,
        expander=expander,
        # Eight and nine: PRD 10's `search_queries`, and the commit that makes a row
        # written inside a request survive it.
        analytics=SearchAnalytics(
            queries=PostgresSearchQueryRepository(session),
            commit=commit if commit is not None else session.commit,
            buffer=buffer,
        ),
        # Ten: the one surface an operator can turn off. It is read here rather
        # than at either boundary because `usher suggest` and
        # `GET /search/suggest` must obey it identically -- a switch honoured
        # by the route and not by the command is a table whose contents depend
        # on which door the keystroke came through.
        suggest_analytics=settings.search_suggest_analytics,
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
    """Build the adapter for one source, or `None` if its credential row has gone missing.

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
    pipeline: Pipeline,
    settings: Settings,
    events: EventPublisher,
    cache: RowCache | None = None,
) -> PushApplyService:
    """One push event into catalog state, through M4's own chain.

    `events` is passed explicitly rather than taken off the pipeline
    because the applier is the one collaborator whose publisher *must* be
    the live bus -- a push merge nobody is told about is the read-through
    loop not closing, which is the milestone.

    `cache` is passed on identical terms and for the identical reason: it is
    process-scoped where the pipeline is session-scoped, and an applier holding
    a cache nobody serves from would invalidate a dict with no reader. `None`
    is a composition root that composes no screens -- `usher sync`, `usher
    work` -- where there is nothing to invalidate.
    """
    return PushApplyService(
        pipeline.ingest,
        pipeline.watch,
        events,
        pipeline.commit,
        cache=cache,
        max_items_per_event=settings.push_max_items_per_event,
    )


def build_enrich_service(
    pipeline: Pipeline,
    settings: Settings,
    provider: MetadataProvider,
    *,
    events: EventPublisher,
    cache: RowCache | None = None,
) -> EnrichService:
    """Enrichment, with its publisher passed in rather than read off the pipeline.

    `events` is explicit for the reason `build_push_applier`'s is, pointing
    the other way: the applier's publisher **must** be the live bus, and this
    one's must **not** be. An enrichment runs inside a job, so its frames are
    `JobWorker`'s to offer once the job's own transaction has committed, and
    `pipeline.events` -- the right answer for every caller outside a job --
    would put them back inside the residual window. Required rather than
    defaulted to `pipeline.events`, because a default is what a sixth caller
    forgets and `mypy` cannot see.
    """
    return EnrichService(
        titles=pipeline.titles,
        episodes=pipeline.episodes,
        payloads=pipeline.payloads,
        provider=provider,
        commit=pipeline.commit,
        events=events,
        # The *same* queue `MatchService` and `IngestService` hold. `services/`
        # may not import `db/`, so nothing below here can discover that these
        # are one table, and a second queue would enqueue index work into an
        # object nothing ever claims from -- enriched titles, no vectors, no
        # error.
        queue=pipeline.queue,
        # Process-scoped where the pipeline is session-scoped, on
        # `build_push_applier`'s terms: `None` is a root that composes no
        # screens (`usher work`, `usher sync`) and has nothing to invalidate.
        cache=cache,
        cache_max_age_days=settings.enrich_cache_max_age_days,
    )


def worker_kinds(
    *,
    provider: MetadataProvider | None,
    embedder: Embedder | None,
    client: LLMClient | None,
) -> frozenset[JobKind]:
    """Which kinds this deployment can run, from build-time facts alone.

    **The one list in `src/` that has to agree with another**, and it is
    deliberately small and deliberately here: `JobWorker` claims
    `list(self._concurrency)` and its concurrency table is keyed by this set,
    while `_worker_handlers` below builds the callables. The two cannot be one
    expression because the handler map needs a `Pipeline` -- i.e. a session --
    and the claimable kinds have to be known before any session is opened.

    Both failure directions are quiet, which is why they get a case rather than
    a comment (`test_composition.py::
    test_every_configuration_registers_exactly_the_kinds_it_claims`, over all
    eight provider/embedder/client configurations): a kind here with no handler
    is a `KeyError` inside a claimed job, and a handler with no entry here is
    work nothing ever claims -- M4's "a queue that grows forever".
    """
    kinds = {
        JobKind.MATCH,
        JobKind.WATCH_HISTORY,
        JobKind.WATCH_WRITEBACK,
        JobKind.SYNC,
        JobKind.BOOTSTRAP,
    }
    if provider is not None:
        kinds |= {JobKind.ENRICH, JobKind.DERIVE}
    if embedder is not None:
        kinds.add(JobKind.INDEX)
    if client is not None:
        kinds.add(JobKind.CURATE)
    return frozenset(kinds)


def worker_concurrency(settings: Settings, kinds: frozenset[JobKind]) -> dict[JobKind, int]:
    """`KIND_CONCURRENCY` resolved against this deployment's global ceiling.

    A `None` there means "whatever the operator configured", and every entry is
    additionally clamped to the global: `USHER_JOB_CONCURRENCY=2` must not be
    quietly overridden to 4 by a per-kind constant chosen for a bigger box.
    """
    return {
        kind: min(settings.job_concurrency, KIND_CONCURRENCY[kind] or settings.job_concurrency)
        for kind in kinds
    }


def build_worker(
    work: UnitOfWork,
    settings: Settings,
    *,
    provider: MetadataProvider | None,
    embedder: Embedder | None,
    client: LLMClient | None,
    registry: "SourceRegistry",
    user_id: uuid.UUID,
    rows: RowCache | None = None,
) -> JobWorker:
    """The queue consumer, with a handler per `JobKind` this process can run."""
    kinds = worker_kinds(provider=provider, embedder=embedder, client=client)

    @asynccontextmanager
    async def scope() -> AsyncIterator[JobScope]:
        async with work() as pipeline:
            # The bus wrapped in a buffer belonging to *this* scope.
            events = DeferredEventPublisher(pipeline.events)
            yield JobScope(
                queue=pipeline.queue,
                commit=pipeline.commit,
                handlers=_worker_handlers(
                    pipeline,
                    settings,
                    provider=provider,
                    embedder=embedder,
                    client=client,
                    registry=registry,
                    user_id=user_id,
                    events=events,
                    # Process-lifetime, so it is carried rather than rebuilt
                    # per scope -- the same terms as `provider`, `embedder`
                    # and `client` above.
                    rows=rows,
                ),
                events=events,
            )

    return JobWorker(
        scope,
        worker_concurrency(settings, kinds),
        max_in_flight=settings.job_concurrency,
        batch_size=settings.job_batch_size,
        lease_seconds=settings.job_lease_seconds,
    )


def scope[T](
    sessions: async_sessionmaker[AsyncSession],
    build: Callable[[AsyncSession], T],
    *,
    commit: bool,
) -> Callable[[], AbstractAsyncContextManager[T]]:
    """One session, one `build(session)`, opened per use.

    Returned as a callable so `usher.services` and `usher.api.lanes` reach a
    database without importing SQLAlchemy, and opened per use so a lane that
    ticks for weeks never holds a session idle in transaction.

    `commit=True` commits after the body, so a raise inside leaves the unit
    uncommitted rather than half-committed. Pass `commit=False` when the built
    object commits on its own cadence.
    """

    @asynccontextmanager
    async def open() -> AsyncIterator[T]:
        async with sessions() as session:
            yield build(session)
            if commit:
                await session.commit()

    return open


def search_query_scope(sessions: async_sessionmaker[AsyncSession]) -> SearchQueryScope:
    """One `SearchQueryRepository` per use, committed on a clean exit.

    The commit is what makes `SearchQueryRetention.run`'s "a commit per chunk"
    true: every repository here flushes and never commits, so a scope that did
    not commit would delete a year of keystrokes inside one transaction and,
    on a process that died mid-loop, delete none of them.
    """
    return scope(sessions, PostgresSearchQueryRepository, commit=True)


def search_query_buffer(sessions: async_sessionmaker[AsyncSession]) -> SearchQueryBuffer:
    """Keystroke rows, written in batches off the request path.

    `search_query_scope`'s unit of work per batch, so N buffered rows cost one
    transaction and one WAL flush rather than N. One per process: the buffer
    outlives every request that submits to it, and the root that builds it owns
    the drain task.
    """
    return SearchQueryBuffer(search_query_scope(sessions))


def similarity_scope(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> SimilarityScope:
    """One `SimilarityService` per use, committing nothing on exit.

    `SimilarityService` is handed the session's own `commit` and calls it per
    page, which is what lets an interrupted walk keep the pages it finished.
    """
    return scope(
        sessions,
        lambda session: SimilarityService(
            PostgresTitleEmbeddingRepository(session),
            PostgresTitleNeighborRepository(session),
            PostgresTitleRepository(session),
            session.commit,
            embedding_model=settings.embedding_model,
        ),
        commit=False,
    )


def build_scheduler(
    settings: Settings, *, sessions: async_sessionmaker[AsyncSession] | None
) -> Scheduler:
    """The scheduled-work loop and its registry."""
    scheduler = Scheduler(tick_seconds=settings.scheduler_tick_seconds)
    if sessions is not None:
        scheduler.register(
            SearchQueryRetention(
                search_query_scope(sessions),
                window=timedelta(days=settings.search_query_retention_days),
                batch=settings.search_query_retention_batch,
                period=RETENTION_PERIOD,
            )
        )
        scheduler.register(
            NeighborRebuildJob(
                similarity_scope(sessions, settings),
                # Hours off the setting rather than a constant: the number this
                # period has to clear is the walk's own duration, which is a
                # function of catalog size.
                period=timedelta(hours=settings.similar_rebuild_period_hours),
            )
        )
    return scheduler


def _worker_handlers(
    pipeline: Pipeline,
    settings: Settings,
    *,
    provider: MetadataProvider | None,
    embedder: Embedder | None,
    client: LLMClient | None,
    registry: "SourceRegistry",
    user_id: uuid.UUID,
    events: DeferredEventPublisher,
    rows: RowCache | None = None,
) -> dict[JobKind, Handler]:
    """One scope's handlers, bound to that scope's repositories.

    Must register exactly `worker_kinds(...)`; see its docstring for why the
    pair needs a case rather than a comment.
    """
    handlers: dict[JobKind, Handler] = {}
    # The resolver is bound to *this* scope's repositories and to the
    # process-lifetime adapter cache. A registry holding the pipeline itself
    # would put two concurrent jobs on one session, because `resolve` issues
    # two reads of its own.
    resolve = registry.bound(pipeline)
    handlers[JobKind.MATCH] = match_handler(pipeline.matcher, pipeline.media_items, resolve)
    handlers[JobKind.WATCH_HISTORY] = watch_history_handler(
        pipeline.watch, resolve, user_id=user_id
    )
    # Unconditional, exactly as MATCH and WATCH_HISTORY are: unlike ENRICH, INDEX,
    # DERIVE and CURATE there is no optional process resource behind a triggered sync,
    # only the adapter factory every root already builds.
    handlers[JobKind.SYNC] = sync_handler(
        pipeline.sources,
        pipeline.reconcile,
        pipeline.watch,
        lambda source: open_adapter(pipeline, source),
        user_id=user_id,
    )
    # Unconditional, joining `MATCH`, `WATCH_HISTORY` and `SYNC`, and in the *same
    # commit* as `JobKind.BOOTSTRAP` itself -- a member with no claimant is the queue
    # that grows forever M4 forbade.
    handlers[JobKind.BOOTSTRAP] = bootstrap_handler(
        lambda phase: run_bootstrap(
            pipeline.bulk,
            pipeline.import_runs,
            pipeline.commit,
            settings,
            phase,
            report=_log_bootstrap_line,
            events=pipeline.events,
        )
    )
    # Unconditional, joining `MATCH`, `WATCH_HISTORY` and `SYNC`: nothing about a write-
    # back is optional.
    handlers[JobKind.WATCH_WRITEBACK] = watch_writeback_handler(
        pipeline.watch_states, pipeline.media_items, resolve, user_id=user_id
    )
    if provider is not None:
        handlers[JobKind.ENRICH] = enrich_handler(
            build_enrich_service(pipeline, settings, provider, events=events, cache=rows)
        )
        # Guarded on the provider rather than on the embedder, and that is the honest
        # dependency rather than the convenient one: `DeriveService` holds a
        # `MetadataProvider` for `to_derivation`, which is a pure mapping and makes no
        # network call.
        handlers[JobKind.DERIVE] = derive_handler(build_derive_service(pipeline, provider))
    # Guarded exactly as ENRICH is, and the symmetry is the point: `run_once` claims
    # only the kinds `worker_kinds` named, so a worker with no model leaves index jobs
    # pending for a worker that has one rather than parking them.
    if embedder is not None:
        handlers[JobKind.INDEX] = index_handler(build_index_service(pipeline, embedder))
    # Guarded exactly as INDEX is, on the client this deployment either has or does not,
    # and the guard is a `mypy` fact rather than a convention: `CurationService` spells
    # its client `LLMClient`, never `LLMClient | None`, so "no client, no curation"
    # cannot be spelled any other way from here.
    if client is not None:
        handlers[JobKind.CURATE] = curate_handler(
            build_curation_service(pipeline, settings, client)
        )
    return handlers


def build_derive_service(pipeline: Pipeline, provider: MetadataProvider) -> DeriveService:
    """One session's repositories plus the provider's pure mapper.

    The `provider` argument looks like `build_index_service`'s `embedder` and
    is a different kind of thing: an embedder is a once-per-*process*
    resource this factory must not build, while a provider is held here only
    for `to_derivation`, which is synchronous and makes no request. Nothing
    on this path opens a socket.
    """
    return DeriveService(
        payloads=pipeline.payloads,
        provider=provider,
        titles=pipeline.titles,
        people=pipeline.people,
        credits=pipeline.credits,
        collections=pipeline.collections,
        images=pipeline.images,
        commit=pipeline.commit,
    )


def build_curation_service(
    pipeline: Pipeline, settings: Settings, client: LLMClient
) -> CurationService:
    """One session's repositories plus the process's completion client."""
    return CurationService(
        pool=pipeline.pool,
        # The *same* `WatchStateRepository` and `TitleRepository` the pool reads from.
        watch_states=pipeline.watch_states,
        titles=pipeline.titles,
        client=client,
        rows=pipeline.curated_rows,
        ledger=pipeline.llm_calls,
        # One commit per generation, covering `replace_for_user` *and* the
        # ledger row: PRD 10's dashboard 5 is `llm_calls JOIN curated_rows
        # USING (generation_id)`, so a commit between them is a window in
        # which a screen exists with no cost attributed to it.
        commit=pipeline.commit,
        model=settings.llm_model,
    )


def build_index_service(pipeline: Pipeline, embedder: Embedder) -> IndexService:
    """One session's repositories plus the process's model.

    The asymmetry in the arguments is the whole design: everything on
    `pipeline` is rebuilt per pass, and the `embedder` is not.
    """
    return IndexService(
        titles=pipeline.titles,
        embeddings=pipeline.embeddings,
        embedder=embedder,
        commit=pipeline.commit,
    )


async def metadata_provider(
    settings: Settings,
) -> tuple[MetadataProvider | None, Callable[[], Awaitable[None]]]:
    """The TMDb provider and the callable that closes its transport."""
    if settings.tmdb_api_key is None:
        # Both kinds named, not just `enrich`: `derive` is registered under
        # this same guard, so naming one would promise an operator that one
        # kind goes unclaimed while two do.
        logger.warning("no TMDb API key configured; enrich and derive jobs will not be claimed")
        return None, nothing
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


async def embedder(
    settings: Settings, *, report: bool = True
) -> tuple[Embedder | None, Callable[[], Awaitable[None]]]:
    """The embedding model and the callable that releases it."""
    if not settings.embedding_enabled:
        if report:
            logger.warning("no embedding model configured; index jobs will not be claimed")
        return None, nothing

    # Before the import, never after: huggingface_hub reads it when it
    # constructs its client, and the failure it prevents names neither the
    # network nor the cache. That ordering is a comment rather than a test
    # because the read happens *inside* the import -- moving this line below
    # `_load_embedder` survives any test that does not load a real model.
    os.environ.setdefault("HF_HUB_OFFLINE", "1" if settings.embedding_offline else "0")
    try:
        built = _load_embedder(settings)
    except (ImportError, OSError) as exc:
        # A missing extra, a missing model file, a cache miss under
        # HF_HUB_OFFLINE=1. All three are a *narrowed* deployment, not a
        # broken one, and all three must be legible -- `str(exc)` for the
        # offline case is the OSError this setting exists to produce.
        if report:
            logger.warning(
                "embedding model unavailable; index jobs will not be claimed: {e}", e=exc
            )
        return None, nothing
    if built.dimension != EMBEDDING_DIMENSIONS:
        # **The check `db/models/search.py` says nothing can make structural.** It is
        # right that nothing makes it structural -- this is a runtime comparison of two
        # numbers, and a `halfvec` typmod is not something the type system can reach.
        await built.aclose()
        if report:
            logger.warning(
                "embedding model {m} is {got} wide and this schema stores "
                "{want}; index jobs will not be claimed",
                m=built.model_name,
                got=built.dimension,
                want=EMBEDDING_DIMENSIONS,
            )
        return None, nothing
    return built, built.aclose


async def llm_client(
    settings: Settings, *, report: bool = True
) -> tuple[LLMClient | None, Callable[[], Awaitable[None]]]:
    """The completion client and the callable that releases it.

    One per process rather than per worker pass, and `(None, no-op)` rather
    than a raise so a deployment without an LLM is *narrowed* rather than
    unstartable -- the shape `embedder` and `metadata_provider` above take,
    and the one place the degradation is reported.

    **Off by default.** Nine of ten row providers need no model, so `GET /home`
    is a shorter screen rather than a broken one; and turning this on sends the
    household's watch history to whatever `USHER_LLM_BASE_URL` names, which may
    be a machine the household does not own.
    """
    if not settings.llm_enabled:
        if report:
            logger.warning("no LLM configured; curate jobs will not be claimed")
        return None, nothing

    if (
        report
        and settings.llm_api_key is not None
        and not settings.llm_price_in_per_mtok
        and not settings.llm_price_out_per_mtok
    ):
        # **Both prices default to zero, so `cost_usd` reads `0.00000000` for
        # an operator who never set them** -- a number that looks like a price
        # and is an absence.
        logger.warning(
            "an LLM credential is configured and USHER_LLM_PRICE_IN_PER_MTOK and "
            "USHER_LLM_PRICE_OUT_PER_MTOK are both unset; llm_calls.cost_usd will "
            "record 0 for every call and PRD 10's spend panel will read flat"
        )

    built = OpenAICompatibleClient(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        price_in_per_mtok=settings.llm_price_in_per_mtok,
        price_out_per_mtok=settings.llm_price_out_per_mtok,
    )
    return built, built.aclose


def image_proxy(
    settings: Settings,
) -> tuple[ImageFetcher, ImageBlobStore, Callable[[], Awaitable[None]]]:
    """The image proxy's two process-scoped halves, and the transport's closer."""
    client = httpx.AsyncClient(timeout=settings.image_fetch_timeout_seconds)
    fetcher = ProviderCdnImageFetcher(
        client,
        base_url=settings.image_cdn_base_url,
        max_bytes=settings.image_max_bytes,
    )
    return fetcher, DiskImageBlobStore(settings.image_cache_dir), client.aclose


def build_image_proxy_service(
    images: ImageRepository, fetcher: ImageFetcher, store: ImageBlobStore
) -> ImageProxyService:
    """One request's `ImageRepository` plus the process's fetcher and store.

    **The same asymmetry `build_index_service` and `build_curation_service`
    have**, and it is why this takes an `ImageRepository` rather than a
    `Pipeline`: the repository is session-scoped and the other two are not.
    It takes the repository directly rather than the pipeline because
    `GET /images/{id}` is a *read* of one row and needs none of the other
    twenty-odd fields — a route that was handed the whole pipeline could reach
    the job queue from a request path, and this one has no business doing so.
    """
    return ImageProxyService(images=images, fetcher=fetcher, store=store)


def _load_embedder(settings: Settings) -> Embedder:
    """The one place a runtime prefix becomes an `Embedder`, isolated so a test can replace it."""
    runtime, separator, _ = settings.embedding_model.partition(":")
    if separator and runtime == OPENAI_RUNTIME:
        from usher.adapters.embedding.openai_compat import OpenAICompatEmbedder

        key = settings.embedding_api_key.get_secret_value()
        return OpenAICompatEmbedder(
            settings.embedding_model,
            base_url=settings.embedding_base_url,
            # Unwrapped at the point of use and handed straight over, never into a local
            # that outlives the call -- CLAUDE.md's SecretStr rule.
            api_key=key or None,
            dimension=EMBEDDING_DIMENSIONS,
            batch_size=settings.embedding_batch_size,
            timeout=settings.embedding_timeout_seconds,
        )
    if separator and runtime != FASTEMBED_RUNTIME:
        raise ValueError(
            f"unknown embedding runtime {runtime!r}; "
            f"expected {FASTEMBED_RUNTIME!r} or {OPENAI_RUNTIME!r}"
        )

    from usher.adapters.embedding.fastembed import FastEmbedEmbedder

    return FastEmbedEmbedder(settings.embedding_model, batch_size=settings.embedding_batch_size)


def build_row_context(pipeline: Pipeline, user: User) -> RowContext:
    """The thirteen values a row may reach, over one unit of work.

    `api/deps.py` assembles the same context from request-scoped dependencies
    and `usher home` from a command's one session; this is the third caller --
    the `rows.refresh` lane, which has neither a request nor a command and only
    a `Pipeline`. It lives here rather than in `api/lanes.py` because that
    module deliberately holds no session and imports no SQLAlchemy, and
    assembling a bag of repositories is wiring.

    **`affinities` is the plain deferred read, not the route's per-request
    memo.** One refresh composes once and `GenreAffinityProvider` awaits it at
    most once, so `api/deps.py:_Affinities`' memo would be a memo with one
    reader -- and the reason the field is a callable at all survives intact: a
    provider that never fires never pays the three statements behind it. Same
    shape `usher home` uses, one file over.
    """
    return RowContext(
        user=user,
        now=lambda: datetime.now(UTC),
        titles=pipeline.titles,
        media_items=pipeline.media_items,
        watch_states=pipeline.watch_states,
        episodes=pipeline.episodes,
        neighbors=pipeline.neighbors,
        people=pipeline.people,
        credits=pipeline.credits,
        collections=pipeline.collections,
        affinities=lambda: pipeline.taste.genre_affinity(user.id),
        curated=pipeline.curated_rows,
        images=pipeline.images,
    )


def unit_of_work(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    events: EventPublisher,
    provider: MetadataProvider | None = None,
    gates: SourceGateRegistry | None = None,
) -> UnitOfWork:
    """One session, one pipeline, one transaction, closed however it ends.

    This is the shape a long-lived lane needs and a command does not: a
    supervisor that held a session would hold it for the life of a socket
    -- hours, idle in transaction, with a snapshot from whenever the lane
    started -- so every unit of work opens its own. Returned as a callable
    so `usher.api.lanes` never imports SQLAlchemy at all, and so a test can
    hand it a pipeline over fakes without standing up a database.

    **The outbound gate registry is resolved once, here, and closed over** --
    not per scope. That is the difference between one gate per source and one
    per unit of work, and this function is where it is decided for three of the
    four composition roots that dial a source: `LaneSupervisor` takes exactly
    one `UnitOfWork` and both the push lane and the worker lane read through
    it, and `usher work` builds one for the daemon. `create_app` passes its own
    registry in so the *request* path can share it too (`app.state`); nobody
    else has a second reader, so nobody else needs to.
    """
    gates = gates if gates is not None else source_gates(settings)

    @asynccontextmanager
    async def open() -> AsyncIterator[Pipeline]:
        async with sessions() as session:
            yield build_pipeline(session, settings, events=events, provider=provider, gates=gates)

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
    """`external_id` -> the configured source that addresses it."""

    def __init__(self) -> None:
        self._adapters: dict[uuid.UUID, SourceAdapter] = {}
        self._building = asyncio.Lock()

    def bound(self, pipeline: Pipeline) -> Callable[[str], Awaitable[SourceBinding | None]]:
        """This registry's resolver, reading through one scope's repositories."""

        async def resolve(external_id: str) -> SourceBinding | None:
            return await self._resolve(pipeline, external_id)

        return resolve

    async def _resolve(self, pipeline: Pipeline, external_id: str) -> SourceBinding | None:
        for source in await pipeline.sources.list_all():
            if not source.enabled:
                continue
            stored = await pipeline.media_items.get_by_external_id(source.id, external_id)
            if stored is None:
                continue
            adapter = await self._adapter_for(pipeline, source)
            if adapter is None:
                return None
            return SourceBinding(source=source, adapter=adapter)
        return None

    async def _adapter_for(self, pipeline: Pipeline, source: Source) -> SourceAdapter | None:
        cached = self._adapters.get(source.id)
        if cached is not None:
            return cached
        async with self._building:
            # Re-read inside the lock: the loser of the race must take the
            # winner's adapter rather than build a second one.
            cached = self._adapters.get(source.id)
            if cached is not None:
                return cached
            adapter = await open_adapter(pipeline, source)
            if adapter is None:
                return None
            self._adapters[source.id] = adapter
            return adapter

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


class SearchGauges:
    """The embedding backlog as PRD 10's two M6 gauges see it."""

    __slots__ = ("_snapshot",)

    def __init__(self) -> None:
        self._snapshot = SearchSnapshot()

    def read(self) -> SearchSnapshot:
        return self._snapshot

    async def refresh(
        self,
        embeddings: TitleEmbeddingRepository,
        neighbors: TitleNeighborRepository,
        model_name: str,
    ) -> None:
        self._snapshot = SearchSnapshot(
            stale=await embeddings.count_stale(model_name),
            refused=await embeddings.count_refused(model_name),
            # The third count is over `title_neighbors` and is the one thing here that
            # is *not* about the embedding backlog.
            neighbors_stale=await neighbors.count_stale(
                # `model_name` is already this method's argument, three
                # lines up: the embedding backlog and the neighbour
                # backlog are counted against the same checkpoint, which
                # is what makes the two gauges answer one question.
                blend_fingerprint=blend_fingerprint(embedding_model=model_name),
            ),
        )


# ---------------------------------------------------------------------------
# The bulk bootstrap, as one dispatch both roots call (PRD 04, M9's E5).
# ---------------------------------------------------------------------------

# Where a phase's own report goes.
BootstrapReporter = Callable[[str], None]


def _log_bootstrap_line(line: str) -> None:
    """The worker's sink.

    One `logger.info` per report line, and `{}` in a dataset name or a tag cannot become
    a loguru placeholder because the line is passed as an argument rather than as the
    format string.
    """
    logger.info("{line}", line=line)


def bulk_client(settings: Settings) -> httpx.AsyncClient:
    """One client for a whole bootstrap run.

    Module-level rather than inline in `run_bootstrap` so a case can observe
    that exactly one is built for a `--phase all` run and that it is closed
    however the run ends. **A client per phase would defeat connection reuse
    across seven datasets, and a client per worker *pass* would be built
    ~17,280 times a day** -- `build_worker`'s own docstring records that
    arithmetic for the same lane, at the same 5 s floor.
    """
    return httpx.AsyncClient(timeout=60.0, headers={"User-Agent": settings.bulk_user_agent})


async def run_bootstrap(
    catalog: BulkCatalogRepository,
    runs: ImportRunRepository,
    commit: Callable[[], Awaitable[None]],
    settings: Settings,
    phase: BootstrapPhase,
    *,
    report: BootstrapReporter,
    events: EventPublisher,
) -> None:
    """PRD 04's phased import, run once, for whichever phases `phase` names."""
    client = bulk_client(settings)
    service = BootstrapService(runs, catalog, commit, events=events, phase=phase)
    try:
        if phase in (BootstrapPhase.IMDB, BootstrapPhase.ALL):
            # The window wraps both IMDb passes, not each separately: the
            # ratings pass writes to the same table, and rebuilding the two
            # ordering indexes between them would pay the cost twice.
            async with catalog.bulk_load_window():
                await service.import_dataset(
                    IMDbTitleDataset(
                        client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                    ),
                    _titles_writer(catalog),
                )
                await service.import_dataset(
                    IMDbRatingDataset(
                        client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                    ),
                    catalog.apply_ratings,
                )
        if phase is BootstrapPhase.RATINGS:
            await _ratings(settings, client, catalog, service, report)
        if phase in (BootstrapPhase.CREDIT_NAMES, BootstrapPhase.ALL):
            await _credit_names(settings, client, catalog, service, report)
        if phase in (BootstrapPhase.ALIASES, BootstrapPhase.ALL):
            await _aliases(settings, client, catalog, service, report)
        if phase in (BootstrapPhase.TMDB_IDS, BootstrapPhase.ALL):
            for kind in (TitleKind.MOVIE, TitleKind.SERIES):
                await service.import_dataset(
                    TMDbIdDataset(
                        client,
                        settings.bulk_data_dir,
                        kind=kind,
                        batch_size=settings.bulk_batch_size,
                    ),
                    catalog.upsert_tmdb_ids,
                )
        if phase in (BootstrapPhase.CROSSWALK, BootstrapPhase.ALL):
            await service.import_dataset(
                WikidataCrosswalkDataset(
                    client,
                    user_agent=settings.bulk_user_agent,
                    endpoint=settings.wikidata_endpoint,
                    batch_size=settings.bulk_batch_size,
                ),
                catalog.upsert_crosswalk,
            )
            await service.link_crosswalk()
        if phase in (BootstrapPhase.MOVIELENS, BootstrapPhase.ALL):
            await _movielens(settings, client, catalog, service, commit, report)
        logger.info("catalog now holds {count} titles", count=await catalog.count_titles())
    finally:
        # In a `finally`, so a phase that raises still gives the connection
        # pool back. One client for every dataset is the whole reason each
        # adapter's own `aclose` is a no-op: closing a shared client from
        # inside one dataset would break its siblings.
        await client.aclose()


def _titles_writer(
    catalog: BulkCatalogRepository,
) -> Callable[[Sequence[ImdbTitle]], Awaitable[int]]:
    """Adapts `upsert_titles`' BulkWriteResult to the `-> int` the service wants.

    The other three repository methods already return `int`, so only this one needs a
    wrapper.
    """

    async def write(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        return result.inserted + result.updated

    return write


async def _ratings(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
    report: BootstrapReporter,
) -> None:
    """`title.ratings.tsv.gz` -> `titles.imdb_*`, and nothing else."""
    if await catalog.count_titles() == 0:
        report(
            "ratings needs a catalog to update: title.ratings is keyed on "
            "imdb_id and titles is empty. Run --phase imdb first."
        )
        return

    await service.import_dataset(
        IMDbRatingDataset(client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size),
        catalog.apply_ratings,
    )


async def _credit_names(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
    report: BootstrapReporter,
) -> None:
    """`name.basics` x `title.principals` -> `titles.credit_names`, and its report."""
    if await catalog.count_titles() == 0:
        report(
            "credit-names needs a catalog to join against: title.principals is "
            "keyed on imdb_id and titles is empty. Run --phase imdb first."
        )
        return

    tally = {"filled": 0, "unmatched": 0, "deferred": 0}

    async def write(rows: Sequence[ImdbCreditNames]) -> int:
        result = await catalog.fill_credit_names(rows)
        tally["filled"] += result.filled
        tally["unmatched"] += result.unmatched
        tally["deferred"] += result.deferred
        return result.filled

    await service.import_dataset(
        IMDbCreditNamesDataset(client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size),
        write,
    )
    _report_credit_names(tally, await catalog.count_titles(), report)


def _report_credit_names(tally: dict[str, int], titles: int, report: BootstrapReporter) -> None:
    """Three lines: what changed, against what, and when to have run it.

    `filled` counts titles whose array actually changed **on this run**, not
    titles seen -- a resumed run reports its own half, and a replay over an
    unchanged dump reports 0 rather than re-reporting the catalog. The
    denominator is the catalog, printed as a count beside the percentage
    because a bare percentage is `0/0` on an empty database and says nothing
    on a small one either.
    """
    report(
        f"credit_names: {tally['filled']} titles filled this run "
        f"({tally['unmatched']} credited titles this catalog does not hold, "
        f"{tally['deferred']} deferred to TMDb)"
    )
    report(f"  {_percent(tally['filled'], titles)} of {titles} titles in the catalog")
    # Precedence, not staleness: the fill writes only skeletons and only
    # non-skeletons are embedded, so it cannot invalidate a vector. What it
    # cannot do is come back for a title TMDb has taken.
    report(
        "  run this BEFORE the TMDb crawl: afterwards every title the crawl "
        "enriched is deferred to TMDb for good and never gains IMDb names"
    )
    if tally["filled"]:
        report("  then: usher index --backfill, and usher similar --rebuild after it")


async def _aliases(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
    report: BootstrapReporter,
) -> None:
    """`title.akas` -> the `alias` half of `title_search_names`."""
    if await catalog.count_titles() == 0:
        report(
            "aliases needs a catalog to compare against: title.akas is keyed on "
            "imdb_id and titles is empty. Run --phase imdb first."
        )
        return

    tally = {"written": 0, "unmatched": 0, "canonical": 0, "duplicate": 0, "read": 0}

    async def write(rows: Sequence[ImdbAka]) -> int:
        result = await catalog.replace_aliases(
            rows, imdb_ids=list(dict.fromkeys(row.imdb_id for row in rows))
        )
        tally["read"] += len(rows)
        tally["written"] += result.written
        tally["unmatched"] += result.unmatched
        tally["canonical"] += result.canonical
        tally["duplicate"] += result.duplicate
        return result.written

    await service.import_dataset(
        IMDbAkaDataset(client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size),
        write,
    )
    _report_aliases(tally, await catalog.count_titles(), report)


def _report_aliases(tally: dict[str, int], titles: int, report: BootstrapReporter) -> None:
    """What was stored, against what was read, and where the rest went.

    **Three rows in four are not aliases at all** and a report that printed
    only `written` would look like a broken import. `canonical` is the
    dominant term -- 5,693,570 of 7,536,366 retained rows (75.5%) restate the
    title's own name under `lower()` -- and an operator watching it sit at ~0
    is watching the comparison miss, which looks exactly like a dump full of
    genuine aliases.
    """
    report(
        f"aliases: {tally['written']} stored this run of {tally['read']} rows read "
        f"({tally['canonical']} restate the title's own name, "
        f"{tally['duplicate']} repeat one already kept, "
        f"{tally['unmatched']} scoped ids matched no title)"
    )
    report(f"  {_percent(tally['written'], tally['read'], noun='rows')} of the rows read")
    report(f"  the catalog holds {titles} titles")


async def _movielens(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
    commit: Callable[[], Awaitable[None]],
    report: BootstrapReporter,
) -> None:
    """The MovieLens tag genome, its vocabulary, and the coverage report."""
    if await catalog.count_titles() == 0:
        report(
            "movielens needs a catalog to join against: the genome is keyed "
            "on imdb_id and titles is empty. Run --phase imdb first."
        )
        return

    dataset = MovieLensGenomeDataset(
        client,
        settings.bulk_data_dir,
        # NOT `settings.bulk_batch_size`. That default is 50,000, sized for
        # ~100-byte rows; a GenomeVector carries 1,128 Python floats (~36 kB),
        # and the whole dataset is 16,376 rows, so 50,000 would yield exactly
        # one ~590 MB batch, committed once, checkpointing nothing -- and a
        # killed run would restart from zero every time.
        batch_size=GENOME_BATCH_SIZE,
    )
    revision = await dataset.revision()

    async def write(rows: Sequence[GenomeVector]) -> int:
        result = await catalog.upsert_genome_vectors(rows, revision=revision)
        _GENOME_TALLY["unmatched"] += result.unmatched
        return result.inserted + result.updated

    _GENOME_TALLY["unmatched"] = 0
    run = await service.import_dataset(dataset, write, revision=revision)
    tags = 0
    if run.status is ImportRunStatus.COMPLETED:
        # The same `revision` the vectors were stamped with, resolved once
        # above -- which is the whole of what makes `genome_tags` and
        # `genome_scores` comparable rather than merely both present.
        vocabulary = await dataset.tag_vocabulary(revision)
        tags = await catalog.replace_genome_tags(vocabulary, revision=revision)
        # `import_dataset` commits its own last batch and then returns, so
        # this write is alone in a fresh transaction and needs its own commit.
        await commit()
    _report_coverage(await catalog.genome_coverage(), _GENOME_TALLY["unmatched"], tags, report)


# The `unmatched` count has nowhere else to go: `BootstrapService.import_dataset` takes
# a writer returning `int` (rows written) and knows nothing about a join's misses.
_GENOME_TALLY = {"unmatched": 0}


def _percent(part: int, whole: int, *, noun: str = "titles") -> str:
    """A percentage, or a sentence when the denominator is zero.

    `noun` names what the denominator counts, because the zero branch prints
    it and this helper now serves three reports over two different
    populations -- `0/0` rendered as *"n/a (0 titles)"* under a line about
    rows read is a wrong sentence rather than a missing one. Defaulted rather
    than required only because the three existing call sites really are
    counting titles.
    """
    return f"n/a (0 {noun})" if whole == 0 else f"{100.0 * part / whole:.2f}%"


def _report_coverage(
    coverage: GenomeCoverage, unmatched: int, tags: int, report: BootstrapReporter
) -> None:
    """Four fractions, the enriched-tier one last because it is the one that matters.

    PRD 05 promised "~7% coverage" and PRD 04 repeated it as "~7% of the
    priority tier", and that figure has never had a denominator. Three of
    these are ceilings the *dataset* can reach; the fourth is what the join
    actually did against this operator's catalog.

    `tags` is how many vocabulary rows this run wrote, `0` when the drain did
    not complete and no vocabulary was loaded. Printed on the same line as the
    vector count because the two are one artefact and a vocabulary that
    silently did not land is the thing an operator most needs to see.
    **Required rather than defaulted to `0`**, so a caller that forgets it is a
    type error rather than a report that quietly says no vocabulary landed --
    the `limit: int = 200` finding in `.claude/rules/testing-discipline.md`,
    one signature over.
    """
    report(f"movielens: {coverage.with_vector} vectors stored ({unmatched} unmatched), {tags} tags")
    report(f"  {_percent(coverage.with_vector, coverage.titles)} of {coverage.titles} titles")
    report(f"  {_percent(coverage.with_vector, coverage.movies)} of {coverage.movies} movies")
    report(
        f"  {_percent(coverage.enriched_with_vector, coverage.enriched)} of the enriched "
        f"tier ({coverage.enriched_with_vector} of {coverage.enriched} titles)"
    )
    # Only when there is more than one. A single-revision table is the normal
    # case and a line reading "revisions: 1" is noise; a table carrying two is
    # a correctness problem `GenomeRepository.get_pair` is already refusing to
    # blend across, and the fix is a re-import.
    if len(coverage.revisions) > 1:
        report("  MIXED RELEASES -- get_pair refuses to compare across these; re-import:")
        for name, count in coverage.revisions:
            report(f"    {name}: {count}")


__all__ = [
    "NO_CREDENTIALS",
    "BootstrapReporter",
    "DefaultUserId",
    "Pipeline",
    "QueueGauges",
    "SearchGauges",
    "SessionFactory",
    "SourceGateRegistry",
    "SourceRegistry",
    "adapter_factory",
    "build_curation_service",
    "build_enrich_service",
    "build_index_service",
    "build_pipeline",
    "build_push_applier",
    "build_row_context",
    "build_scheduler",
    "build_worker",
    "bulk_client",
    "embedder",
    "llm_client",
    "metadata_provider",
    "nothing",
    "open_adapter",
    "run_bootstrap",
    "search_query_buffer",
    "search_query_scope",
    "selected_sources",
    "source_gates",
    "unit_of_work",
]
