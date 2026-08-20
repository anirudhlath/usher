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

**Why this module is in no import-linter contract's source list.** Seven
contracts exist and none names `usher.composition`, deliberately: it imports
`usher.db` and `usher.adapters`, so a core module reaching it breaks
contracts two and three -- which report indirect chains by default, unlike
contracts six and seven's `allow_indirect_imports = true`. Verified by
planting `from usher.composition import Pipeline` in
`usher/services/push.py`: two contracts break. So the hole an unlisted module
would otherwise leave is closed by what this module itself imports rather
than by a rule.

**And that is also why it is absent from contract seven's sources** ("no
concrete search or embedding implementation escapes its package", M6). This
module is where `PostgresSearchIndex`, `PostgresSuggestIndex` and
`FastEmbedEmbedder` are named on purpose; `allow_indirect_imports = true` is
what keeps the real chain `usher.api.lanes -> usher.composition ->
usher.adapters.search.postgres` KEPT while a *direct* import in
`usher.services` or `usher.api` stays BROKEN. Both halves measured rather
than argued -- without the flag that real chain reports BROKEN.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

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
from usher.services.search import SearchAnalytics, SearchService
from usher.services.similar import SimilarityService, blend_fingerprint
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
    # M2's two bulk-import ports, on the pipeline since M9's E5 for the
    # reason every other port here is: `run_bootstrap` is one dispatch two
    # roots call, and `build_worker` sees a `Pipeline` and nothing else. The
    # names are `bulk`/`import_runs` rather than `catalog`/`runs` because
    # `runs` above is already `sync_runs` and two fields called `runs` on one
    # dataclass is exactly how a caller reaches the wrong table.
    #
    # ⚠️ `BulkCatalogRepository.bulk_load_window` **commits the caller's
    # session** -- the port's one documented exception -- and asks for a
    # session with no unrelated pending work on it. That holds for both
    # callers today: `usher bootstrap` opens a session for the command, and
    # `JobWorker` commits a claim before it runs a handler (ADR-0033). A
    # third caller that shared this session with a half-written unit of work
    # would have it committed underneath.
    bulk: BulkCatalogRepository
    import_runs: ImportRunRepository
    queue: JobQueue
    embeddings: TitleEmbeddingRepository
    neighbors: TitleNeighborRepository
    taste_rows: TasteRepository
    # M8's table, and the field is here because `usher home` assembles a
    # `RowContext` from this pipeline exactly as `api/deps.py` assembles one
    # from its request-scoped dependencies -- `CuratedProvider` reads
    # `list_for_user` through the context and a CLI that could not fill that
    # field would compose a screen the route does not. `build_curation_service`
    # is the *write* half, and it reaches this same field: one table, one
    # object, which is what stops a generation landing somewhere nothing serves
    # from.
    curated_rows: CuratedRowRepository
    # M8's cost ledger. Write-only from here -- nothing in `src/` reads it
    # back, and PRD 10's spend dashboards are SQL against the table -- so it
    # is on the pipeline for the reason every other port is: `services/` may
    # not import `db/` (ADR-0009), and a `CurationService` handed a ledger of
    # its own would attribute a real charge to an object nobody reads.
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
    # M9's overrides table, and the field is here because the registry above is
    # only half of "which providers compose". `usher home` and the API's
    # `rows.refresh` lane both build a `HomeService` from this pipeline, and a
    # provider an operator disabled through `PUT /admin/rows/providers/{slug}`
    # must be absent from both -- a setting honoured by one composer and not
    # the other is two different products, and the lane's half is the sharper
    # one: a background refresh composing the unfiltered registry writes the
    # disabled shelf straight back into the screen cache the route just
    # cleared.
    row_provider_settings: RowProviderSettingsRepository
    events: EventPublisher
    commit: Callable[[], Awaitable[None]]


def source_gates(settings: Settings) -> SourceGateRegistry:
    """This process's outbound rate gates, one per source.

    **Built once at a composition root and handed down**, which is the whole
    of ADR-0039 §4 and the reason `adapter_factory` below takes it rather than
    reading the rate itself. `create_app`'s lifespan builds one and puts it on
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
    """This deployment's tuning, applied to every adapter it builds.

    One function rather than three constructions, because a knob added to
    the registry has to reach the server, the CLI and the lanes at once --
    `push_stale_after_seconds` reaching two of the three is a source whose
    staleness window depends on which process opened its socket.

    **`gates` is required rather than defaulted**, and that is the type
    checker doing the work a convention would not: this function is called
    once per unit of work, so a caller that forgot the registry would get a
    fresh gate per pipeline and the multiplication ADR-0039 §4 records would
    come straight back. There is no spelling of this call that silently
    re-introduces it.

    **It is *this* call that is enforced and not the chain below it, which is
    worth stating because the flattering reading is available.**
    `ConfiguredSourceAdapterFactory(gates=None)`, `EmbySession(limiter=None)`
    and `EmbyAdapter(limiter=None)` are all defaulted, deliberately: each means
    "nobody configured this" and gets a private registry at the unlimited rate,
    which is what lets a test build one directly (ADR-0039's Consequences say
    so). The cost of tightening the first of the three is small and measured --
    **2 edits**, `tests/unit/test_adapters_factory.py`'s two bare
    constructions, out of four sites there of which two already pass `gates=` --
    and it is still declined, because a required argument at that layer buys
    nothing this layer does not already hold: every path that reaches an adapter
    in a running process comes through here.
    """
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

    `embedder` is `None` on the same terms, and for a sharper reason: it is a
    once-per-*process* resource and this function runs once per session, so it
    is **never built here**. `usher search --mode fused` builds one with
    `composition.embedder(settings)` and closes it in the same `finally`;
    every other caller passes nothing and gets a `SearchService` whose
    full-text and trigram lanes work exactly as well. That is the whole of
    ADR-0022's "the embedder is optional" at the wiring layer.

    `llm` is `None` on the same terms, and is `None` by default twice over:
    `USHER_LLM_ENABLED` is `false`, so `composition.llm_client` answers
    `(None, no-op)` even for a caller that asks.

    **A client is necessary and not sufficient**, which is the whole of the two
    switches at the wiring layer. `USHER_QUERY_EXPANSION_ENABLED` is `false`
    even on a deployment that has turned the LLM on, because the measurement
    behind it is about retrieval quality rather than about cost (PRD 05), so an
    operator who wants curated rows does not thereby want their queries
    rewritten. Both conditions are live: a client is absent on every default
    deployment, and it is *present and unused here* on every deployment that
    curates without expanding -- which is the ordinary M8 shape. Given both,
    this builds the `QueryExpansionService` that sits in front of
    `SearchService`'s embed, over **this session's** `llm_calls` and **this
    session's** commit, because a ledger row written through anything else is
    spend recorded in a transaction nobody commits. Given either one missing,
    `SearchService` gets no expander and every line of the search path is what
    M6 shipped.

    **The client reaches nothing else from here.** The other consumer of a
    completion is `CurationService`, which `build_curation_service` composes
    from this pipeline *plus* the client -- deliberately a second factory,
    because everything on a pipeline is rebuilt per worker pass and the client
    is not.
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
    # default.** `CandidatePoolService` then gets `None` from
    # `TasteService.centroid` and returns the base order whole -- M8's boundary
    # call 5, and the reason the pool is built from signals that need no model.
    # Built as a local rather than inline for one reason: `Pipeline.taste` and
    # `CandidatePoolService.taste` must be the *same* service, or a household
    # would have two definitions of its own taste and the stored centroid would
    # be written twice per generation.
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
        # pipeline.** `None` means "nobody handed me one", which is one
        # command's single pipeline (`usher sync`) or a directly-built pipeline
        # in a test -- there is nothing for it to share a gate *with*, so a
        # private registry is the honest answer. Every caller that opens more
        # than one pipeline passes the same registry to all of them, and
        # `unit_of_work` is what makes that automatic for the three lanes.
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
        # **Delegated to `build_search_service` rather than spelled here**, so
        # this deployment's search tuning has exactly one assembly. M9 gave
        # `GET /search` a request-scoped `SearchService` that never wants the
        # ingest graph around it; assembled twice, the two would be two chances
        # for `search_result_limit`, `search_rrf_k` or the ef_search GUC to
        # reach one caller and not the other -- and the drift would be silent,
        # because both spellings return a working `SearchService`.
        #
        # The **expander** is built here and passed down, because it is the one
        # collaborator that is not a function of `settings` alone: an expansion
        # is billed to `llm_calls` and committed, both of which are
        # per-session, while the client is per-process. This function is the
        # only place that holds one of each.
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
        # zero vector, every consumer drops the signal (ADR-0014), and
        # `genre_affinity` is unaffected because it reads counts rather than
        # vectors -- which is the whole reason Task 23 declines PRD 06's
        # "taste centroid concentrated in a genre".
        # **The one deployment fact a provider is told about.** Same
        # `embedder is None` test `SearchService` uses, and the same one
        # `TasteService` acts on: with no embedder `title_neighbors` holds
        # genre and keyword overlap alone (M6's blend drops the absent cosine
        # term rather than zeroing it), so "Because you watched Dune" is a
        # causal claim nothing computed and the sentence softens.
        row_providers=row_providers(semantic=embedder is not None),
        row_provider_settings=PostgresRowProviderSettingsRepository(session),
        taste=taste,
        # The pool is the whole of M8's retrieval half, and its size is the
        # prompt's token budget -- **~20.4 prompt tokens a candidate**,
        # measured 2026-08-07 against the *shipped* prompt at four pool sizes:
        # the marginal cost is 20.40 tokens/candidate from 8 -> 200 and 20.45
        # from 200 -> 600. This comment read *"~14.6, measured"* until then,
        # which was ADR-0028's 2,924-token figure divided by 200 -- a *total*
        # divided by a count, taken from a probe prompt that rendered a
        # candidate as name and year. The shipped line adds the genre list
        # (`curation_prompt._genres`), which is the whole +40%. One model, one
        # tokenizer, one evening: `gemma-4-26b-a4b`. This is
        # `USHER_CURATION_POOL_SIZE`'s one reader.
        #
        # **What a per-candidate ownership marker would add, measured
        # 2026-08-11 in the same way, because this is the comment that invites
        # the question.** Same endpoint, same model and therefore the same
        # tokenizer -- `usage.prompt_tokens` reported by a local vLLM serving
        # `cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit` (`max_model_len` 16,384), pool
        # 200, `max_tokens=1`, four completions. Rendering *"owned"* /
        # *"not owned"* on every candidate line costs **2.900** tokens a
        # candidate and *"in the library"* / *"not in the library"* costs
        # **4.900** -- i.e. 14.2% and 24.0% on top of the 20.40 above. Neither
        # ships: M9 Task G3 declared a 2.0 tokens/candidate bar before
        # measuring and both missed it, and at pool 600 (this endpoint's
        # measured ceiling, 12,540 prompt tokens) even the cheap one leaves
        # 56 tokens under `max_model_len` once `llm_max_output_tokens` is
        # added. Correcting the *opening sentence* instead costs **+26 tokens,
        # once**, and that is what shipped. ADR-0028's 2026-08-11 amendment.
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
) -> SearchService:
    """PRD 05's read path on one session, and nothing else.

    **Narrow on purpose.** `GET /search` needs a `SearchService` per request
    and needs none of the ingest graph, so a route reaching `build_pipeline`
    would construct a matcher, a reconciler, a watch-state syncer, a
    similarity service, ten row providers and a candidate pool -- for every
    keystroke-adjacent request -- to reach one field of the result. This
    builds eight objects.

    **The indexes are built here rather than being handed in**, for the
    reason `build_pipeline` gave when it held this code: nothing outside
    `SearchService` has any business holding a `SearchIndex`. PRD 05's split
    is retrieve-then-rank, and a caller that could reach the generator
    directly would get unranked hits with no `owned` flag and no
    `SearchAnswer` to say what ran.

    **Three indexes rather than two since M9's B5**, because the suggest path
    is two of them: `PostgresPrefixSuggestIndex` is tier 1 and
    `PostgresSuggestIndex` is tier 2, and `GET /search/suggest?tier=` picks
    between them per request (ADR-0031). Both are built here and neither is
    conditional -- `m09a` creates the two `text_pattern_ops` btrees
    unconditionally, so there is no deployment where one tier exists and the
    other does not, and an optional one would be a `?tier=prefix` request with
    no honest answer. This is the *one* assembly of them: a route wiring its
    own would be a second wiring that returns a working `SearchService`, which
    is the silent drift this function exists to prevent.

    **The household reaches `search` as an argument, never as a collaborator
    bound here.** What this function wires is the *repository* the watch-state
    term reads through; which household a given search speaks for is a property
    of the request, and a `SearchService` built around one would be a
    per-household object on a per-session factory.

    `embedder` is **passed and never built here**, and that is ADR-0022 at the
    wiring layer rather than an omission: it is a once-per-*process* resource
    and this function runs once per *session*, so a model constructed here
    would be one per request on the route and one per command on the CLI.

    ✅ **Both roots now pass one, which is issue #31** (M9 group B's open
    question 4, closed). `usher search --mode semantic|fused` builds one for
    the command and closes it in the same `finally`; `create_app`'s lifespan
    builds one per process and parks it on `app.state`, and
    `api/deps.get_search_service` reads it from there. `None` is still the
    default and still the whole answer for a deployment that configured no
    model -- `?mode=semantic` is then a 422 naming the missing capability and
    `?mode=fused` narrows.

    *(This paragraph read "`None` for every caller but `usher search`" and gave
    a 65 MB ONNX session as the reason the API had none. The size is real and
    is an argument about **building**, not about passing; it is also
    runtime-dependent and predates the second runtime -- `openai:` is an HTTP
    client holding no model.)*

    `expander` is passed rather than built for the reason above it: it needs a
    `LLMCallRepository` and a commit on *this* session plus an `LLMClient`
    that outlives the session, and only `build_pipeline` holds both. Every
    other caller gets `None` and every line of the search path is M6's --
    which is also the shipped default twice over (`USHER_LLM_ENABLED` is
    `false`, and `USHER_QUERY_EXPANSION_ENABLED` is `false` even when it is
    not, because expansion measured *worse*: MRR 0.733 -> 0.373, PRD 05).

    **The analytics pair is built here and not passed, unlike the expander**,
    because both halves are functions of this session alone -- which is the
    same test the two suggest indexes and the watch-state repository already
    pass. That is what makes `search_queries` written on all three roots
    without any of them saying so: `api/deps.get_search_service` and
    `usher search` reach this function, and `build_pipeline` delegates to it
    rather than assembling its own. **`session.commit` rather than the
    caller's commit boundary** -- `api/deps.get_session` has one and
    `cli._session_for` does not, so a row left for the caller to commit is a
    row `usher search` silently loses (F2).
    """
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
        # Six and seven, on the same terms, and they are what makes the taste
        # term reachable from a request. Neither needs an embedder: the
        # centroid is *read* from `user_taste` (whatever process computed it)
        # and the vectors are read scoped to the model that row names. A route
        # assembling these itself would be a second wiring, which is the drift
        # this function exists to prevent -- silent, because both spellings
        # return a working `SearchService`.
        PostgresTasteRepository(session),
        PostgresTitleEmbeddingRepository(session),
        result_limit=settings.search_result_limit,
        embedder=embedder,
        expander=expander,
        # Eight and nine: PRD 10's `search_queries`, and the commit that makes
        # a row written inside a request survive it. Both are functions of the
        # session, so this is the one assembly of them -- a second caller
        # wiring its own would be a second chance for one to arrive without the
        # other, which is precisely the state `SearchAnalytics` exists to make
        # unconstructible.
        analytics=SearchAnalytics(
            queries=PostgresSearchQueryRepository(session), commit=session.commit
        ),
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
    pipeline: Pipeline, settings: Settings, provider: MetadataProvider, *, events: EventPublisher
) -> EnrichService:
    """Enrichment, with its publisher passed in rather than read off the
    pipeline.

    `events` is explicit for the reason `build_push_applier`'s is, pointing
    the other way: the applier's publisher **must** be the live bus, and this
    one's must **not** be. An enrichment runs inside a job, so its frames are
    `JobWorker`'s to offer once the job's own transaction has committed
    ([ADR-0033](../prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)),
    and `pipeline.events` -- the right answer for every caller outside a job
    -- would put them back inside the residual window. Required rather than
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
        # The *same* queue `MatchService` and `IngestService` hold. This is
        # what a composition root is for: `services/` may not import `db/`
        # (ADR-0009), so nothing below here can discover that these are one
        # table, and a second queue would enqueue index work into an object
        # nothing ever claims from -- enriched titles, no vectors, no error.
        queue=pipeline.queue,
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
) -> JobWorker:
    """The queue consumer, with a handler per `JobKind` this process can run.

    Shared because `usher work` and the server's worker lane must register
    the *same* handlers: a lane that quietly lacked `enrich` would leave a
    demand-promoted job at the head of the queue forever, and the client
    that promoted it watching an SSE stream that never fires.

    **It takes a `UnitOfWork`, not a `Pipeline`, and that is the whole of M9's
    W1.** `AsyncSession` is not concurrency-safe and every repository a handler
    holds is bound to one, so a worker running jobs concurrently needs a
    session, a commit, a handler set and an event buffer *per job* -- which
    means this is a factory rather than a bound assembly. `_worker_handlers`
    below is called once per scope, and the process-lifetime resources
    (`provider`, `embedder`, `client`, and `registry`'s adapter cache) are the
    ones that must **not** be rebuilt there.

    **`provider is None` is not reported here**, and the reason survives the
    change: `metadata_provider` is where the decision is made and every
    composition root calls it exactly once per process, while this factory's
    scopes are opened once per *job*. A `logger.warning` here would have been
    ~17,280 lines a day when it ran once a pass and is worse now.
    """
    kinds = worker_kinds(provider=provider, embedder=embedder, client=client)

    @asynccontextmanager
    async def scope() -> AsyncIterator[JobScope]:
        async with work() as pipeline:
            # The bus wrapped in a buffer belonging to *this* scope. A service
            # built below whose frames belong to the job's unit of work is
            # handed this, so a frame raised inside a job is offered after
            # `complete()` and its commit (ADR-0033) -- and a concurrent job's
            # `discard()` cannot empty it, which one shared buffer would.
            #
            # **That is the default and not a law, and there are three
            # exceptions with one reason between them.** The push and reconcile
            # lanes are not wrapped because they are not jobs; the `bootstrap`
            # registration is not wrapped although it *is* one, and its own
            # comment below carries the argument. All three commit their own
            # subject before they publish and all three publish per batch, so
            # deferring them buys nothing and costs the whole point of the
            # frame.
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
) -> dict[JobKind, Handler]:
    """One scope's handlers, bound to that scope's repositories.

    Must register exactly `worker_kinds(...)`; see its docstring for why the
    pair needs a case rather than a comment.
    """
    handlers: dict[JobKind, Handler] = {}
    # The resolver is bound to *this* scope's repositories and to the
    # process-lifetime adapter cache. `SourceRegistry` used to hold the
    # pipeline and be `rebind`-ed once a pass; holding one under concurrent
    # jobs would have put two of them on the same session through the door
    # nobody was looking at, since `resolve` issues two reads of its own.
    resolve = registry.bound(pipeline)
    handlers[JobKind.MATCH] = match_handler(pipeline.matcher, pipeline.media_items, resolve)
    handlers[JobKind.WATCH_HISTORY] = watch_history_handler(
        pipeline.watch, resolve, user_id=user_id
    )
    # Unconditional, exactly as MATCH and WATCH_HISTORY are: unlike ENRICH,
    # INDEX, DERIVE and CURATE there is no optional process resource behind a
    # triggered sync, only the adapter factory every root already builds.
    # `open_adapter` is a module-level function rather than a method so it
    # can be shared with `usher.cli._open_adapter`'s reporting wrapper; bound
    # here to this pipeline the way `resolve` already is above.
    handlers[JobKind.SYNC] = sync_handler(
        pipeline.sources,
        pipeline.reconcile,
        pipeline.watch,
        lambda source: open_adapter(pipeline, source),
        user_id=user_id,
    )
    # Unconditional, joining `MATCH`, `WATCH_HISTORY` and `SYNC`, and in the
    # *same commit* as `JobKind.BOOTSTRAP` itself -- a member with no claimant
    # is the queue that grows forever M4 forbade. There is no optional process
    # resource behind a bulk import: `USHER_BULK_DATA_DIR` and an outbound
    # client are things every deployment has, and whether the directory is
    # *writable* is a run-time answer recorded on `import_runs`, not a
    # build-time absence like a TMDb key or an embedding model.
    #
    # **The sink is `logger.info`, never `print`.** `usher bootstrap` renders
    # the same sentences to a terminal; a worker inside the server process
    # renders them to the log, which is the only difference between the two
    # roots and the reason `run_bootstrap` takes a sink at all.
    #
    # ⚠️ **`pipeline.events` and deliberately NOT the scope's buffer, which is
    # the opposite of every registration below and the same call the push and
    # reconcile lanes already make.** `DeferredEventPublisher` holds a job's
    # frames until `complete()` and its commit, and its own docstring sizes
    # the buffer for "a handful of events at most" -- a bootstrap raises one
    # per committed batch, **26 for `--phase imdb`'s title pass alone** at the
    # shipped 50,000 batch size, so deferring them delivers the whole progress
    # bar as a single jump after the run it was describing has already
    # finished. Two further reasons, either sufficient: those batches are
    # *individually* committed by this handler (no transaction spans the work
    # -- `JobWorker` commits the claim before the handler runs), so ADR-0033's
    # ordering rule is already satisfied at the publish site and the buffer
    # buys nothing; and `discard()` on a failing job would throw away frames
    # naming rows that really did land. `test_composition.py` pins the choice
    # from both sides, because no unit case of `JobWorker` can see which
    # publisher a handler was handed.
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
    # Unconditional, joining `MATCH`, `WATCH_HISTORY` and `SYNC`: nothing
    # about a write-back is optional. The four guarded registrations below
    # each rest on a collaborator a deployment may not have -- a TMDb key, an
    # embedding model, an LLM endpoint -- and this one needs only the
    # session's own repositories and the resolver every source-scoped kind
    # already takes. A guard here would leave a client's own watch write
    # pending forever on the shipped default deployment -- M4's "a job kind
    # whose handler is a stub is a queue that grows forever", arriving as a
    # registration rather than as a missing function.
    handlers[JobKind.WATCH_WRITEBACK] = watch_writeback_handler(
        pipeline.watch_states, pipeline.media_items, resolve, user_id=user_id
    )
    if provider is not None:
        handlers[JobKind.ENRICH] = enrich_handler(
            build_enrich_service(pipeline, settings, provider, events=events)
        )
        # Guarded on the provider rather than on the embedder, and that is the
        # honest dependency rather than the convenient one: `DeriveService`
        # holds a `MetadataProvider` for `to_derivation`, which is a pure
        # mapping and makes no network call. A deployment with no key has no
        # TMDb payloads to derive from at all -- they exist only because a key
        # once did -- so leaving derive jobs pending for a worker that has one
        # is exactly INDEX's bargain, one lane over.
        handlers[JobKind.DERIVE] = derive_handler(build_derive_service(pipeline, provider))
    # Guarded exactly as ENRICH is, and the symmetry is the point: `run_once`
    # claims only the kinds `worker_kinds` named, so a worker with no model leaves index
    # jobs pending for a worker that has one rather than parking them. A job
    # parked that way needs a human to release it, and its only problem was
    # being offered to the wrong process. A deployment without the extra
    # still has full-text and trigram over all 1.27M titles -- narrowed, not
    # broken.
    if embedder is not None:
        handlers[JobKind.INDEX] = index_handler(build_index_service(pipeline, embedder))
    # Guarded exactly as INDEX is, on the client this deployment either has or
    # does not, and the guard is a `mypy` fact rather than a convention:
    # `CurationService` spells its client `LLMClient`, never `LLMClient | None`,
    # so "no client, no curation" cannot be spelled any other way from here.
    # The *member* `JobKind.CURATE` is unconditional -- two things outside the
    # worker need the vocabulary, the enqueue site and `depth()`'s promise of a
    # key per kind -- and only the registration moves, which is what leaves
    # curate work pending for a process that can run it rather than parking
    # work whose only problem was the process it was offered to.
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
    """One session's repositories plus the process's completion client.

    **The same asymmetry `build_index_service` has**, and for the same reason:
    everything on `pipeline` is rebuilt per worker pass and the `client` is
    not. `llm_client` builds one per *process* -- an `httpx.AsyncClient` with
    its own connection pool, rebuilt every 5 s by `lanes._run_worker`
    otherwise -- exactly as `embedder` does for a 65 MB ONNX session.

    **`client` is `LLMClient`, never `LLMClient | None`**, which is the whole
    of why this factory is reached only from `build_worker`'s guard.
    `composition.llm_client` already answers `(None, no-op)` with a warning
    for `USHER_LLM_ENABLED=false`, so the composition root is the one layer
    that can know a deployment has no model -- and spelling the parameter
    non-optional makes "no client, no curation" something `mypy` enforces
    there instead of a `self._client is None` branch unreachable from `src/`.

    **`model` is `settings.llm_model` and is not defaulted.** It is the same
    string `OpenAICompatibleClient` was built with a few lines up, and it is
    the only honest value for `llm_calls.model` on the path where no response
    came back to read one from. A default here would be a second value that
    silently disagrees with the client's.

    `min_cards` is deliberately **not** wired to a setting.
    `USHER_CURATION_MIN_CARDS` was planned and never shipped;
    `curation_validate.DEFAULT_MIN_CARDS` is the one definition, and it
    crosses the prompt, the schema and the validator, so a second copy on
    `Settings` would be a fourth place for the three to disagree. The day an
    operator needs it, it lands with a reader and an `.env.example` line in
    the same commit.
    """
    return CurationService(
        pool=pipeline.pool,
        # The *same* `WatchStateRepository` and `TitleRepository` the pool
        # reads from. This is what a composition root is for: `services/` may
        # not import `db/` (ADR-0009), so nothing below here can discover that
        # the household's history and its candidate pool are two sides of one
        # table -- and a second pair would let the prompt recommend what the
        # household just finished.
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
    """The TMDb provider and the callable that closes its transport.

    Returns `(None, no-op)` when no key is configured, rather than raising:
    **five of the seven job kinds need no provider at all** -- `match`,
    `watch_history`, `index`, `curate` and `watch_writeback` -- so a worker
    that refused to start without a TMDb key would take five working kinds
    down with the two that need one. (`derive` is the second: `build_worker` registers it under
    the same `provider is not None` guard as `enrich`, because a derivation
    reads the payload that enrichment cached.)

    One client per *process*, not per job or per pass, because the token
    bucket that keeps this deployment under TMDb's ~40 rps ceiling lives on
    the client. A client per job would give every job its own budget, which
    is a rate limiter that limits nothing.

    **This is also where a missing key is reported**, for the same reason:
    once per process. Not a silent skip -- PRD 08's "TMDb key missing"
    degradation is a *narrowed* deployment, and an operator whose enrich
    queue never drains has to be able to see why -- but not `build_worker`'s
    either, which runs once per worker pass and made that one sentence
    ~17,280 warnings a day. A push-only deployment never reaches this
    function at all (`create_app` and `usher push` call it only when
    `worker_enabled`), which is correct: with no worker there are no enrich
    jobs to leave unclaimed.
    """
    if settings.tmdb_api_key is None:
        # Both kinds named, not just `enrich`: `derive` has been registered
        # under this same guard since M7 and this sentence still promised an
        # operator that one kind would go unclaimed while two did.
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
    """The embedding model and the callable that releases it.

    **Deliberately the same shape as `metadata_provider` above**, down to the
    return type, for the same three reasons.

    *One per process, never per pass.* `build_worker` is called once per
    worker pass -- `usher.api.lanes._run_worker` rebuilds it every turn of a
    loop whose floor is `IDLE_SLEEP_SECONDS = 5.0`. A 65 MB ONNX load there is
    4.84 s cold and 0.13 s warm, every five seconds, forever, with nothing in
    the logs saying so. The precedent is on record in this repository at
    ~17,280 log lines a day for a *string*; a model is not a string.

    *`(None, no-op)` rather than a raise.* **`index` is the only one of the
    six job kinds that needs a model**; `match`, `watch_history`, `enrich`,
    `derive` and `curate` need none, and PRD 05's catalog-lookup tier -- the
    one serving 1.27M titles -- needs none either. A worker refusing to start
    without one would take five working kinds down with the sixth, and a
    `create_app` that did would turn a missing extra into a server that will
    not boot.

    *This is where the degradation is reported, once.* Not `build_worker`, for
    the reason its own docstring gives: an operator whose index queue never
    drains has to be able to see why, and a per-pass warning is how an
    operator learns to ignore warnings.

    **`report=False` is for the callers that are not a worker root**, and the
    first of them was found by an operator smoke run rather than by the suite.
    The sentences below are about a *lane* -- "index jobs will not be claimed"
    -- which is exactly right for `usher work`, the server's worker lane and
    `usher push`, and wrong for a process that claims nothing. There are two
    such callers now: `usher search`, and `create_app`'s lifespan under
    `USHER_WORKER_ENABLED=false`, which since issue #31 builds a model for the
    *search routes* rather than for a lane (`report=settings.worker_enabled`
    there, so the one server shape that does claim index jobs still says so).
    Against `usher search` the sentence is wrong twice over: it advises about
    work that
    process does not do, and `cli._print_home_report`'s rule says an operator's
    report is printed rather than logged, so with `USHER_LOG_JSON=true` (the
    default) it is a JSON envelope in front of the search results. That rule
    was cited as `cli.py:153-154` in two places until 2026-08-07, by which
    point those lines held an `httpx.AsyncClient` construction inside
    `_bootstrap`. `_search` prints its
    own line, which names the setting and the extra instead of a lane, so the
    information is not lost -- it is better. Pinned by
    `tests/integration/test_cli_pipeline.py::
    test_every_search_command_prints_and_never_logs`, which drives `--mode
    fused` for exactly this reason: a version of that case using only
    `full_text` never reaches this function at all.

    **The `fastembed` import is local**, the way `connect_websocket` imports
    `websockets`: `usher.composition` is imported by every entry point
    including `usher bootstrap-status`, and this dependency lives behind an
    extra (167 MiB, 28 packages, no torch -- against sentence-transformers'
    4.8 GiB and 59, ~4.5 GiB of it GPU runtime pulled unconditionally on a
    host that may never have a GPU).

    **`HF_HUB_OFFLINE` is set before that import and it is not optional.**
    Measured: warm cache, no network, flag unset -> `RuntimeError: Cannot send
    a request, as the client has been closed`, from huggingface_hub reusing a
    closed client on its retry path, in a message naming neither the network
    nor the cache. Reproduced two independent ways. It is also the only
    setting under which a genuine cache miss produces a comprehensible
    `OSError`. `setdefault`, so an operator warming the cache once -- or a
    container that set it -- wins over this default.
    """
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
        # **The check `db/models/search.py` says nothing can make structural.**
        # It is right that nothing makes it structural -- this is a runtime
        # comparison of two numbers, and a `halfvec` typmod is not something
        # the type system can reach. What it buys is *where* the mismatch is
        # reported: without it, a deployment configured with a 384-wide
        # checkpoint against `m09e`'s 1024-wide column discovers the problem
        # one failed `index` job at a time, in a worker log, with the width
        # named by neither side -- asyncpg's error is about a vector.
        #
        # **Narrowed rather than fatal, matching the branch above.** A wrong
        # width is a misconfiguration and not an absent capability, which
        # argues for a raise; the deciding fact is that the *consequence* is
        # identical -- no model, index jobs unclaimed, nine of ten row
        # providers and the whole catalog-lookup tier unaffected. Refusing to
        # boot would take a working deployment down over a setting only the
        # index lane reads.
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

    **Deliberately the same shape as `embedder` and `metadata_provider`
    above**, down to the return type, and for the same reasons: one per
    process rather than per worker pass, `(None, no-op)` rather than a raise
    so a deployment without an LLM is *narrowed* rather than unstartable, and
    this is the one place the degradation is reported.

    **Off by default is the honest default twice over here.** Nine of ten row
    providers need no model, so `GET /home` is a shorter screen rather than a
    broken one -- that is `embedding_enabled`'s argument. The second reason is
    this project's only one of its kind: turning this on sends the household's
    watch history to whatever `USHER_LLM_BASE_URL` names, which may be a
    machine the household does not own. A default that curated out of the box
    would make that something an operator discovers rather than chooses.

    **No lazy import and no extra**, unlike the embedder. There is nothing to
    import lazily -- the client is httpx, which every entry point already
    loads -- which is the whole of ADR-0027 arriving as an absence.
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
        # an operator who never set them** -- a number that looks like a
        # measurement and is an absence. For the local vLLM this milestone was
        # verified against, zero *is* the honest value; on a paid endpoint it
        # is spend billing invisibly, and PRD 10's spend dashboard is flat in
        # both cases.
        #
        # **Gated on the credential, and that gate is the whole design of this
        # warning.** `test_a_configured_llm_is_built_and_says_nothing` states
        # the rule it would otherwise break: *a warning every
        # correctly-configured deployment sees is a warning nobody reads*, and
        # a self-hosted endpoint with no prices is correctly configured -- it
        # is the shipped shape of this milestone. An `llm_api_key` is what
        # separates the two populations: a hosted provider requires one and
        # the local vLLM this was measured against needs none. So the sentence
        # only reaches the deployment it is actually about.
        #
        # A warning and not a refusal, because zero is legitimate. Here rather
        # than in a `Settings` validator for the reason the TMDb line above
        # moved here: this function is where the decision is *made*, and each
        # of the three composition roots calls it exactly once per *process*
        # -- which is what keeps a per-process fact out of a per-pass log at
        # 17,280 lines a day.
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
    """The image proxy's two process-scoped halves, and the callable that
    closes the fetcher's transport.

    **Deliberately not the `(None, no-op)` shape `llm_client`, `embedder` and
    `metadata_provider` share, and the difference is the point.** Those three
    answer `None` because a deployment without a model, an embedder or a TMDb
    key is *narrowed* rather than broken. There is no switch here and nothing
    to be missing: the proxy needs no credential (ADR-0032 — the CDN is
    unauthenticated), takes no dependency this project did not already have,
    and its only inputs are a directory and a URL that both have defaults. A
    nullable return would be a degradation nothing can cause.

    **One `httpx.AsyncClient` per process**, for `metadata_provider`'s reason
    one layer over: a client per request is a connection pool per request, and
    the pool is the entire benefit of keeping one. Its timeout is
    `image_fetch_timeout_seconds` — an order of magnitude below the LLM's,
    because this one is on a request path.

    **No throttle, unlike `TmdbClient`.** The image CDN publishes no rate limit
    and is not the API the ~40 rps ceiling is about; a token bucket here would
    be a limiter invented against a number nobody has measured. The real bound
    is the cache: after the first request per `(image, rung)` there is no
    outbound traffic at all.

    **M10's S3 re-examined this against `USHER_SOURCE_REQUESTS_PER_SECOND`'s
    new gate and confirmed it rather than reversing it.** That gate exists
    because a media *source* is a machine somebody is watching television on
    (ADR-0039, issue #19); `image.tmdb.org` is a CDN and the argument above is
    untouched by it. The decline is one of five the S3 enumeration recorded,
    and this paragraph is where the code says so -- `image.tmdb.org` is the
    upstream, the cache is the bound, and
    `tests/unit/test_outbound_call_sites.py` is the closed table that makes a
    *new* unthrottled call site a red rather than a discovery.

    The store is returned rather than built per request because
    `DiskImageBlobStore` holds a `Path` and nothing else — but it is returned
    *here*, beside the fetcher, so a deployment cannot end up with a cache
    directory the fetcher's byte ceiling was never told about.
    """
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
    """The one place a runtime prefix becomes an `Embedder`, isolated so a test
    can replace it.

    Absolute import, so the sibling-named module
    `usher.adapters.embedding.fastembed` does not shadow the third-party
    `fastembed` -- Python 3 absolute imports make that correct, and the
    adapter's own docstring records that it was *verified* rather than
    assumed.

    **Both class imports stay local, and as of `m09e` that buys less than it
    reads as buying.** The reason on record is `embedder`'s -- this module is
    imported by every entry point including `usher bootstrap-status`, and
    `fastembed` lives behind an extra. But the dispatch below needs each
    runtime's `RUNTIME` string, and those are imported at module scope, so
    both adapter *modules* are already loaded by the time anything calls this.
    Measured 2026-08-13: importing `usher.adapters.embedding.fastembed` pulls
    in **none** of `fastembed`, `huggingface_hub`, `onnxruntime`, `torch` or
    `tokenizers` -- the third-party import is inside `FastEmbedEmbedder.
    __init__`, which is what the deferral was really protecting and what still
    protects it. So these two lines are now a convention rather than a saving,
    and the `HF_HUB_OFFLINE`-before-import ordering `embedder` documents is
    untouched: nothing reads that variable until a model is constructed.

    **Two runtimes since `m09e`, and an unknown prefix raises.** The
    alternative -- fall back to `fastembed` -- is the worst available
    behaviour: `USHER_EMBEDDING_MODEL=openia:BAAI/bge-m3` would embed the
    catalog with whatever `fastembed` made of the string and write
    `openia:BAAI/bge-m3` into `title_embeddings.model_name`, so the
    fingerprint would record a model that never ran and the stale predicate
    would report the deployment as current. A typo has to be loud here
    precisely *because* the fingerprint is trusted everywhere else.

    A bare checkpoint with no recognised prefix is **not** an unknown runtime
    -- it is `fastembed`, matching `checkpoint_of`'s own leniency in both
    adapters, so an operator who wrote `BAAI/bge-large-en-v1.5` gets the model
    rather than a startup failure.
    """
    runtime, separator, _ = settings.embedding_model.partition(":")
    if separator and runtime == OPENAI_RUNTIME:
        from usher.adapters.embedding.openai_compat import OpenAICompatEmbedder

        key = settings.embedding_api_key.get_secret_value()
        return OpenAICompatEmbedder(
            settings.embedding_model,
            base_url=settings.embedding_base_url,
            # Unwrapped at the point of use and handed straight over, never
            # into a local that outlives the call -- CLAUDE.md's SecretStr
            # rule. Empty means "send no Authorization header", which is the
            # local-server case, so it is normalised to `None` here rather
            # than inside the adapter: the adapter should not have to know
            # that this project spells "absent" as an empty `SecretStr`.
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

    **This registry holds the adapter cache and deliberately holds no
    pipeline.** It used to hold one and be `rebind`-ed once a worker pass, on
    the argument that repositories change every few seconds while connection
    pools must not. That argument is still right and the *shape* was wrong the
    moment jobs became concurrent: `resolve` issues two reads of its own
    (`sources.list_all` and `media_items.get_by_external_id`), so two jobs
    resolving at once would have put two coroutines on one `AsyncSession`
    through a door nobody was looking at -- not the handler's repositories,
    which the per-job scope already separates, but the resolver's. `bound()`
    takes the scope's pipeline as an argument instead, which makes the split a
    signature rather than a convention.

    **Adapter construction is behind a lock**, because it is the one `await`
    in `resolve` that mutates the cache: without it two jobs for the same
    source both miss, both authenticate, and one of the two adapters is
    overwritten in the dict and never closed -- a leaked socket per race,
    which is exactly the kind of thing that only appears under load.
    """

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
    """The embedding backlog as PRD 10's two M6 gauges see it.

    `QueueGauges`' shape exactly, and for the same reason: an OTel observable
    callback runs on the metric reader's background thread and cannot await an
    asyncpg query, so `read()` hands back the last *complete* re-read and
    `refresh` is a coroutine the caller awaits where awaiting is safe --
    `register_search_gauges`' docstring carries the whole argument. Stale,
    never wrong.

    `refresh` takes the repository rather than holding one, because a worker
    pass's session lives for one pass while this snapshot outlives every pass.
    It takes the model name for the same reason `usher index` does: staleness
    is a question about a *name*, which is what recording `model_name` on the
    row bought, and a gauge computed against a different name from the one the
    backfill sweeps would report a number nothing acts on.

    Two `count(*)`s per refresh, both over `enrichment_state <> 'skeleton'` --
    boundary call 4's population and exactly `ix_titles_enrichment_state`'s
    partial predicate, so the scan is the enriched tier (2k-10k rows) rather
    than the 1.27M-row catalog. That is what makes a per-pass refresh at the
    worker's 5 s floor affordable.
    """

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
            # The third count is over `title_neighbors` and is the one thing
            # here that is *not* about the embedding backlog. It is refreshed
            # in the same pass because it is read from the same session and
            # answers the same operator question -- "is my derived state
            # current" -- and `blend_fingerprint()` is resolved here rather
            # than passed in so there is exactly one definition of the running
            # blend, which is the whole of ADR-0020's argument.
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

#: Where a phase's own report goes. `usher bootstrap` passes `print`; the
#: `bootstrap` job handler passes a loguru sink, because a worker inside the
#: server process writing to stdout is not a report, it is noise in a log
#: aggregator.
#:
#: A sink rather than a returned structure, deliberately: every one of these
#: lines is a *sentence* an operator reads -- "run this BEFORE the TMDb crawl",
#: "MIXED RELEASES -- get_pair refuses to compare across these" -- and a
#: structure would mean each root re-rendering the same prose, which is the
#: second copy this whole extraction exists to prevent. What a machine reads is
#: `import_runs`, through `GET /admin/bootstrap/status`.
BootstrapReporter = Callable[[str], None]


def _log_bootstrap_line(line: str) -> None:
    """The worker's sink. One `logger.info` per report line, and `{}` in a
    dataset name or a tag cannot become a loguru placeholder because the line
    is passed as an argument rather than as the format string.
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
    """PRD 04's phased import, run once, for whichever phases `phase` names.

    **One dispatch, two callers.** `usher bootstrap` held the whole of this
    from M2 until M9, and `POST /admin/bootstrap/{phase}` needs the same
    thing: a handler that re-implemented it would be a second dispatch that
    drifts, which `api/deps.py` already argues in the other direction (*"a
    composition root is the thing that has to agree with the other one"*).
    This module is the one both roots already share and the one permitted to
    import `usher.db` and `usher.adapters`; no router names it, so the eighth
    import contract is untouched.

    **It takes ports and a `commit`, not a session and not an engine.** The
    engine, the `session_factory` and the process lifetime stay with the
    caller -- `cli._bootstrap` owns one session per command and
    `composition.unit_of_work` owns one per unit of work -- which is the same
    line every other factory in this module draws. It also means the whole
    dispatch is drivable over fakes, which is what makes "the CLI and the
    handler run the same phases in the same order" a case rather than a
    claim.

    **The order is `BootstrapPhase`'s order and three of its edges are
    measured**; the enum's own docstring carries the evidence, and the one
    that costs an operator real money is that `credit-names` belongs *before*
    a TMDb crawl. Two structural facts here are load-bearing for the same
    reason and are asserted rather than trusted:

    - **`bulk_load_window()` wraps *both* IMDb passes, not each.** The ratings
      pass writes to the same table, so rebuilding `ix_titles_sort_name` and
      `ix_titles_name_lower_year` between them pays for the rebuild twice --
      measured at 35.8 s suspended against 40.2 s kept (**11.0% faster**) with
      a rebuilt pair **~24% smaller** (97 MB against 127 MB),
      `.claude/rules/bootstrap-and-datasets.md`.
    - **`link_crosswalk()` runs after the crosswalk import and only after
      it.** The import writes the pairs; the link is what attaches them to
      `titles`, and a crosswalk phase that skipped it stores rows nothing
      reads.

    🔴 **`bulk_load_window()` engages only on an empty `titles`, and on a
    *serving* process that guard is now load-bearing rather than incidental.**
    It drops two indexes and rebuilds them under a `SHARE` lock, so on a live
    catalog the no-op is what keeps `POST /admin/bootstrap/imdb` from taking
    browse ordering away from every reader for the length of a rebuild.
    Asserted in `tests/integration/test_admin_bootstrap.py`, not assumed.

    Nothing here raises for an upstream failure: `BootstrapService.
    import_dataset` records a `FAILED` `ImportRun` and returns, which is what
    lets `--phase all` continue past one dead upstream and what makes
    `import_runs` -- not the queue -- the durable record of a bootstrap. A
    `bootstrap` job therefore *completes* even when its phase failed, exactly
    as a `sync` job does when `ReconcileService` records a `FAILED` `SyncRun`.

    **`events` is required and is where the `bootstrap.progress` frames go.**
    `usher bootstrap` passes a real `NullEventPublisher` -- a separate process
    with no SSE client on the other side of a publish, the same answer
    `usher work` already gives for `title.updated` -- and `build_worker`
    passes the **process bus**, deliberately not `JobWorker`'s deferred
    buffer. That polarity is the opposite of `enrich`'s and the reason is in
    `build_worker`'s own comment: this producer commits its own subject per
    batch, and deferring a per-batch frame behind a multi-thousand-batch load
    turns a progress bar into a single jump at the end.
    """
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
    """Adapts `upsert_titles`' BulkWriteResult to the `-> int` the service
    wants. The other three repository methods already return `int`, so only
    this one needs a wrapper."""

    async def write(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        return result.inserted + result.updated

    return write


async def _credit_names(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
    report: BootstrapReporter,
) -> None:
    """`name.basics` x `title.principals` -> `titles.credit_names`, and the
    report that says how much of the catalog gained a name.

    **This phase is why weight class B of `search_document` has anything in
    it for a title TMDb has never reached**, and it makes **no API call at
    all**. T3 measured the `people` + `credits` entity design at 2.702 GB
    against a 2.0 GB ceiling and refused it, so no person and no credit row is
    written here: the join is resolved in the adapter and what lands is a
    `text[]` on a column that already exists.

    **Run it before the TMDb crawl, not after, and the reason is precedence
    rather than staleness.** `fill_credit_names` writes only where
    `enrichment_state = 'skeleton'`, so TMDb owns every title it has reached
    and this phase defers on it. That same guard is why the fill **cannot
    stale an embedding**: `db/repositories/search.py:180` pins the embedded
    population to `enrichment_state <> 'skeleton'`, the exact complement of
    what this writes, so the two sets are disjoint by construction and a
    title this phase touches has no vector to invalidate.

    What running it late costs is **coverage, and it is not recoverable by
    re-running the phase** -- which is why it still earns a line on the
    operator's own terminal. Every title the crawl enriches is one this phase
    then declines, on that run and on every future one, so the names simply
    never arrive: of the **204,335 titles with >=100 votes**, the **203,969
    (99.82%)** that would have gained a `credit_names` are left with whatever
    `DeriveService` extracted from TMDb's own payload and no IMDb fallback at
    all. Run first, nothing is lost either way -- a later derivation
    overwrites IMDb's names with TMDb's for exactly the titles TMDb covers.

    **This paragraph said the opposite until 2026-08-12**, and so did five
    other statements including the two an operator reads. An audit caught it
    against the `AND m.ours` predicate one file over; the ordering was right
    and the argument for it was not.

    **The precondition is checked before the dataset is constructed**, for
    `_movielens`' reason and against a much larger download: 308 MB of
    `name.basics` plus 778 MB of `title.principals`. Against an empty catalog
    every row would match nothing, the run would checkpoint `COMPLETED`, and
    every later `--phase all` would find that checkpoint and do nothing -- a
    permanent, invisible failure. No `ImportRun` is created, because the
    absence of a row is what `bootstrap-status` renders as "this phase has not
    run".

    **What a resume costs is not free and is worth knowing before killing
    one:** `BulkCursor.position` is a line offset into `title.principals`
    only, and the `nconst -> primaryName` index is rebuilt from the whole of
    `name.basics` on every run, resumed or not -- a measured **19.5 s and
    345 MiB** before the first batch, with the `title.principals` pass a
    further 157 s.
    """
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
    """`title.akas` -> the `alias` half of `title_search_names`.

    **This is the alias source M6 refused that table for the lack of**, and
    like `credit-names` it costs no API call: TMDb's `alternative_titles` is
    in neither `append_to_response` list, so aliases are in `raw_payloads` at
    all only if the crawl's request shape changes, and this dump needs no such
    change.

    **The scope handed to `replace_aliases` is the batch's own titles**, in
    first-seen order, and the port asks for it as a separate argument for a
    reason this caller cannot fully honour: a title whose akas IMDb has
    *withdrawn* contributes no row, so no batch names it and its stale aliases
    stand. A streaming importer has no other scope available -- the
    alternative is one call naming all 1.27M titles, which is not a batch --
    and the withdrawal is repaired by a re-import only for titles that still
    have at least one aka. Worth knowing before reading a stale alias as a
    bug in the writer.

    **A title's rows all reach one call**, which is `IMDbAkaDataset.group_of`'s
    whole job: `replace_aliases` deletes by scope before it inserts, so a
    split title would have its first half deleted by its second half's call --
    silently, because both halves are inside their own call's scope. Measured
    over the pinned dump: **924 of 924 batch boundaries** would land inside a
    title and **3,867 rows** would be written and then deleted.

    The empty-catalog precondition is `_credit_names`' and `_movielens`', for
    the same reason and against a 486 MiB download.
    """
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
    """The MovieLens tag genome, its tag vocabulary, and the coverage report
    that is the actual deliverable of this phase.

    **The precondition is checked before the dataset is constructed, and the
    outcome it prevents is the worst one available here.** Run against an
    empty catalog, `import_dataset` would download 350,896,731 B, stream
    18,472,128 rows, write 0, checkpoint `COMPLETED`, and `bootstrap-status`
    would show a green phase. Every later `--phase all` would then find a
    completed checkpoint at the file's end and do nothing, so the failure
    would be **permanent and invisible**. PRD 08 says every operator command
    has to work against an empty database -- and "work" means saying why, not
    succeeding vacuously.

    Three properties of the refusal, each deliberate:

    - **It refuses before the download.** 335 MiB is the cost of finding out
      late.
    - **It creates no `ImportRun`.** A `FAILED` row would be a lie -- nothing
      failed upstream -- and a `COMPLETED` one would be worse. The absence of
      a row is the honest state, and it is what `bootstrap-status` already
      renders as "this phase has not run".
    - **It refuses only on an *empty* catalog.** A non-empty catalog whose
      join still matches nothing is not an error, it is a *number*, and the
      coverage report below is where it becomes visible. Refusing on a
      coverage threshold would be inventing a policy; 1.82% of movies is the
      expected shape rather than a fault.

    In `--phase all` the precondition is unreachable in the normal case; it
    exists for the operator who runs `--phase movielens` alone against a
    fresh database.

    **Measured end to end on 2026-08-04** against a real
    `pgvector/pgvector:pg17` holding a real `--phase imdb` bootstrap
    (1,271,570 titles): 16,376 movie runs consumed, **15,565 vectors stored,
    811 unmatched**, in **23.8 s** wall clock with the archive already
    cached. The 811 are genome movies whose IMDb id the catalog does not
    hold -- 5.0% of the genome -- because M2 retains only four `titleType`s
    and MovieLens carries some it drops. That is the join's miss count doing
    exactly the job it exists for.

    **A re-run does NOT report updates, and the plan predicted it would.**
    The first run checkpoints at `position = 16376`, so the second resumes
    from a *completed* cursor, skips every run, yields no batch, and writes
    nothing -- 14.7 s of re-parsing to do nothing, and `0 unmatched` because
    the writer is never called. That is correct and is the same shape
    `--phase imdb` already has; the insert-vs-update distinction lives in the
    repository and is covered there, not through a second CLI invocation.

    **The tag vocabulary is written after the drain and only on a COMPLETED
    run, and both halves are decisions.**

    *After*, because before it would have to `ensure_local` outside
    `import_dataset`'s `except UsherPortError`. Afterwards the archive is
    already local and the only failures left are a parse and the database --
    which still matters, because the parse failure is a `PortDataMalformed`
    and that family is deliberately **not** in `OPERATOR_ERRORS` (ADR-0026's
    2026-08-07 amendment put the transport half in and left the content half
    to keep its stack). The download half of the original argument no longer
    applies: an unreachable `files.grouplens.org` raises `PortUnavailable`,
    which is now a sentence wherever it is raised.

    *Only on COMPLETED*, because a vocabulary is what explains the vectors and
    a failed drain has not finished writing them. The run that eventually
    completes writes it.

    **This is also the upgrade path, and it is the reason "after" is not a
    problem.** A catalog bootstrapped under M7 has a *completed*
    `movielens.genome` checkpoint and no vocabulary at all: re-running the
    phase resumes from that cursor, yields no batch, writes no vector -- and
    still reaches this, because the run it returns is `COMPLETED`.

    **`run.rows_written` is the wrong predicate, and not for the reason it
    looks like.** It is *cumulative across resumes*:
    `PostgresImportRunRepository.start()` keeps it when the revision has not
    moved, `BootstrapService._drain` adds each batch's count to the stored
    one, and an archive that *has* moved resets it to 0 and then re-imports
    every row. So on the upgrade path above it reads truthy and writes the
    vocabulary anyway -- measured 2026-08-07, `if run.rows_written:` in place
    of this line passes all 2,883 unit and all 899 integration cases. The two
    spellings differ only for a *completed* run that has never written a
    vector, which is a catalog holding no genome movie at all, and there a
    vocabulary explains nothing. `COMPLETED` is the honest predicate because
    "the drain finished" is the question being asked; the defect worth
    guarding against is a **per-run** tally, which does leave the M7 upgrade
    without a vocabulary and which
    `test_a_completed_checkpoint_that_writes_no_vector_still_loads_the_vocabulary`
    fails on.
    """
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


# The `unmatched` count has nowhere else to go: `BootstrapService.import_dataset`
# takes a writer returning `int` (rows written) and knows nothing about a
# join's misses. A module-level tally rather than a wider port change, because
# a join's miss count is this one phase's report and not a property of every
# bulk import -- and the alternative, widening the writer's return type, would
# touch all four existing call sites for one caller's benefit.
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
    """Four fractions, the enriched-tier one last because it is the one that
    matters.

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
    "SourceGateRegistry",
    "SourceRegistry",
    "adapter_factory",
    "build_curation_service",
    "build_enrich_service",
    "build_index_service",
    "build_pipeline",
    "build_push_applier",
    "build_row_context",
    "build_worker",
    "bulk_client",
    "embedder",
    "llm_client",
    "metadata_provider",
    "nothing",
    "open_adapter",
    "run_bootstrap",
    "selected_sources",
    "source_gates",
    "unit_of_work",
]
