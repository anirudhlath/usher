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

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.adapters.search.postgres import PostgresSearchIndex, PostgresSuggestIndex
from usher.adapters.tmdb import TmdbClient, TmdbMetadataProvider
from usher.config import Settings
from usher.db.repositories.collection import PostgresCollectionRepository
from usher.db.repositories.credentials import PostgresCredentialStore
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
from usher.db.users import ensure_default_user
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.ports.credentials import CredentialStore
from usher.ports.embedding import Embedder
from usher.ports.events import EventPublisher, NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import (
    CollectionRepository,
    CreditRepository,
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
from usher.ports.rows import RowProvider
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.derive import DeriveService
from usher.services.enrich import EnrichService
from usher.services.handlers import (
    SourceBinding,
    derive_handler,
    enrich_handler,
    index_handler,
    match_handler,
    watch_history_handler,
)
from usher.services.index import IndexService
from usher.services.ingest import IngestService
from usher.services.jobs import JobWorker
from usher.services.matching import MatchService
from usher.services.push import PushApplyService
from usher.services.reconcile import ReconcileService
from usher.services.rows import ROW_PROVIDERS
from usher.services.search import SearchService
from usher.services.similar import SimilarityService
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
    queue: JobQueue
    embeddings: TitleEmbeddingRepository
    neighbors: TitleNeighborRepository
    taste_rows: TasteRepository
    people: PersonRepository
    credits: CreditRepository
    collections: CollectionRepository
    adapters: SourceAdapterFactory
    matcher: MatchService
    ingest: IngestService
    reconcile: ReconcileService
    watch: WatchStateSyncService
    search: SearchService
    similar: SimilarityService
    taste: TasteService
    # The registry itself, not a list assembled here. A provider enabled by
    # *registration in code* is boundary call 9, and a list a composition
    # root builds by hand is a list the tenth provider is forgotten from --
    # which is dead code that looks exactly like a provider with nothing to
    # say. `services/rows/__init__.py` owns it; this field is the wiring.
    row_providers: tuple[RowProvider, ...]
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
    embedder: Embedder | None = None,
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
    queue = PostgresJobQueue(
        session,
        max_attempts=settings.job_max_attempts,
        backoff_seconds=settings.job_backoff_seconds,
    )
    people = PostgresPersonRepository(session)
    credits = PostgresCreditRepository(session)
    collections = PostgresCollectionRepository(session)
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
        embeddings=embeddings,
        neighbors=neighbors,
        taste_rows=taste_rows,
        people=people,
        credits=credits,
        collections=collections,
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
        # The two indexes are built here rather than being fields on the
        # pipeline, because nothing outside this service has any business
        # holding a `SearchIndex`: PRD 05's split is retrieve-then-rank, and a
        # caller that could reach the generator directly would get unranked
        # hits with no `owned` flag and no `SearchAnswer` to say what ran.
        search=SearchService(
            PostgresSearchIndex(
                session,
                ef_search=settings.search_hnsw_ef_search,
                rrf_k=settings.search_rrf_k,
            ),
            PostgresSuggestIndex(
                session,
                threshold=settings.search_trigram_threshold,
                candidates=settings.search_suggest_candidates,
            ),
            titles,
            media_items,
            result_limit=settings.search_result_limit,
            embedder=embedder,
        ),
        # **No embedder here, in either form.** The rebuild reads stored
        # vectors and never embeds anything, which is why `usher similar`
        # starts in 0.13 s rather than paying a 4.84 s cold model load --
        # and why a deployment with no embedding extra can still read and
        # rebuild neighbours for whatever the worker did index.
        similar=SimilarityService(embeddings, neighbors, titles, session.commit),
        # **The embedder is passed and may be `None`, which is the shipped
        # default.** `TasteService.centroid` then returns `None` rather than a
        # zero vector, every consumer drops the signal (ADR-0014), and
        # `genre_affinity` is unaffected because it reads counts rather than
        # vectors -- which is the whole reason Task 23 declines PRD 06's
        # "taste centroid concentrated in a genre".
        row_providers=ROW_PROVIDERS,
        taste=TasteService(
            watch_states=watch_states,
            embeddings=embeddings,
            titles=titles,
            taste=taste_rows,
            embedder=embedder,
            now=lambda: datetime.now(UTC),
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
        # The *same* queue `MatchService` and `IngestService` hold. This is
        # what a composition root is for: `services/` may not import `db/`
        # (ADR-0009), so nothing below here can discover that these are one
        # table, and a second queue would enqueue index work into an object
        # nothing ever claims from -- enriched titles, no vectors, no error.
        queue=pipeline.queue,
        cache_max_age_days=settings.enrich_cache_max_age_days,
    )


def build_worker(
    pipeline: Pipeline,
    settings: Settings,
    *,
    provider: MetadataProvider | None,
    embedder: Embedder | None,
    resolve: Callable[[str], Awaitable[SourceBinding | None]],
    user_id: uuid.UUID,
) -> JobWorker:
    """The queue consumer, with a handler per `JobKind` this process can run.

    Shared because `usher work` and the server's worker lane must register
    the *same* handlers: a lane that quietly lacked `enrich` would leave a
    demand-promoted job at the head of the queue forever, and the client
    that promoted it watching an SSE stream that never fires.

    **`provider is None` is not reported here**, and the reason is that this
    function is called once per worker *pass*: `usher.api.lanes._run_worker`
    rebuilds the worker on each turn of a loop whose floor is
    `IDLE_SLEEP_SECONDS`, so a `logger.warning` here was ~17,280 identical
    lines a day in the default no-key deployment -- measured at exact 5 s
    intervals. The degradation is still surfaced, once, by
    `metadata_provider`, which is where the decision is made and which every
    composition root calls exactly once per process.
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
        # Guarded on the provider rather than on the embedder, and that is the
        # honest dependency rather than the convenient one: `DeriveService`
        # holds a `MetadataProvider` for `to_derivation`, which is a pure
        # mapping and makes no network call. A deployment with no key has no
        # TMDb payloads to derive from at all -- they exist only because a key
        # once did -- so leaving derive jobs pending for a worker that has one
        # is exactly INDEX's bargain, one lane over.
        worker.register(JobKind.DERIVE, derive_handler(build_derive_service(pipeline, provider)))
    # Guarded exactly as ENRICH is, and the symmetry is the point: `run_once`
    # claims `list(self._handlers)`, so a worker with no model leaves index
    # jobs pending for a worker that has one rather than parking them. A job
    # parked that way needs a human to release it, and its only problem was
    # being offered to the wrong process. A deployment without the extra
    # still has full-text and trigram over all 1.27M titles -- narrowed, not
    # broken.
    if embedder is not None:
        worker.register(JobKind.INDEX, index_handler(build_index_service(pipeline, embedder)))
    return worker


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
        commit=pipeline.commit,
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
    `match` and `watch_history` jobs need no provider at all, and a worker
    that refused to start without a TMDb key would take two working lanes
    down with the third.

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
        logger.warning("no TMDb API key configured; enrich jobs will not be claimed")
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

    *`(None, no-op)` rather than a raise.* `match`, `enrich` and
    `watch_history` need no model, and PRD 05's catalog-lookup tier -- the one
    serving 1.27M titles -- needs none either. A worker refusing to start
    without one would take three working lanes down with the fourth, and a
    `create_app` that did would turn a missing extra into a server that will
    not boot.

    *This is where the degradation is reported, once.* Not `build_worker`, for
    the reason its own docstring gives: an operator whose index queue never
    drains has to be able to see why, and a per-pass warning is how an
    operator learns to ignore warnings.

    **`report=False` is for the one caller that is not a worker root**, and it
    was found by an operator smoke run rather than by the suite. The sentence
    below is about a *lane* -- "index jobs will not be claimed" -- which is
    exactly right for `usher work`, the server's worker lane and `usher push`,
    and is wrong twice over for `usher search`: it advises about work that
    process does not do, and `cli.py:153-154`'s rule says an operator's report
    is printed rather than logged, so with `USHER_LOG_JSON=true` (the default)
    it is a JSON envelope in front of the search results. `_search` prints its
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
    return built, built.aclose


def _load_embedder(settings: Settings) -> Embedder:
    """The one line that touches `fastembed`, isolated so a test can replace it.

    Absolute import, so the sibling-named module
    `usher.adapters.embedding.fastembed` does not shadow the third-party
    `fastembed` -- Python 3 absolute imports make that correct, and the
    adapter's own docstring records that it was *verified* rather than
    assumed.
    """
    from usher.adapters.embedding.fastembed import FastEmbedEmbedder

    return FastEmbedEmbedder(settings.embedding_model, batch_size=settings.embedding_batch_size)


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

    async def refresh(self, embeddings: TitleEmbeddingRepository, model_name: str) -> None:
        self._snapshot = SearchSnapshot(
            stale=await embeddings.count_stale(model_name),
            refused=await embeddings.count_refused(model_name),
        )


__all__ = [
    "NO_CREDENTIALS",
    "DefaultUserId",
    "Pipeline",
    "QueueGauges",
    "SearchGauges",
    "SourceRegistry",
    "adapter_factory",
    "build_enrich_service",
    "build_index_service",
    "build_pipeline",
    "build_push_applier",
    "build_worker",
    "embedder",
    "metadata_provider",
    "nothing",
    "open_adapter",
    "selected_sources",
    "unit_of_work",
]
