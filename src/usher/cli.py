"""Command-line composition root: `python -m usher <command>`.

The second composition root alongside `api/`. It is the only module allowed
to construct adapters, repositories, and services together, which is why
`pyproject.toml` carries a contract forbidding anything from importing it.

PRD 08 says first run "offers bootstrap through the admin API -- it does not
start a multi-hour download unprompted". The admin API arrives with the rest
of the HTTP surface in M9; this CLI is that trigger until then, and it has
the same property: nothing downloads unless an operator asks.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.movielens import GENOME_BATCH_SIZE, MovieLensGenomeDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.api.lanes import LaneSupervisor
from usher.composition import (
    NO_CREDENTIALS,
    DefaultUserId,
    Pipeline,
    QueueGauges,
    SearchGauges,
    SourceRegistry,
    build_derive_service,
    build_pipeline,
    build_worker,
    embedder,
    llm_client,
    metadata_provider,
    nothing,
    open_adapter,
    selected_sources,
    unit_of_work,
)
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.users import default_user, ensure_default_user
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.bulk import GenomeVector, ImdbTitle
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.ports.repository import BulkCatalogRepository, GenomeCoverage
from usher.ports.rows import RowContext
from usher.ports.search import SearchFilters, SearchMode
from usher.ports.source import SourceAdapter
from usher.services.bootstrap import BootstrapService
from usher.services.home import ComposeReport, HomeService
from usher.services.rows.cache import RowCache
from usher.services.search import SemanticSearchUnavailable
from usher.telemetry import (
    configure_telemetry,
    register_queue_gauges,
    register_search_gauges,
)

# `movielens` runs **last** in `--phase all`: the genome joins to `titles` on
# `imdb_id`, and an empty catalog joins to nothing. After `crosswalk` too --
# it costs nothing and keeps the tuple in execution order, which is what an
# operator reads it as.
PHASES = ("imdb", "tmdb-ids", "crosswalk", "movielens", "all")
# The two lanes `ReconcileService` walks `list_items` for. `watch_state` is a
# real `SyncRunKind` and is deliberately absent: `sync` always runs it after
# the item walk, so offering it as an *alternative* would let an operator ask
# for a run that walks `list_items` and labels itself a lane the sweep then
# declines to act on.
SYNC_KINDS = ("full", "delta")
# How long `work` waits after a pass that claimed nothing. Not a setting: it
# is the polling floor of a lane that already has push (M5) as its real
# answer, and a knob would invite tuning a number that is about to stop
# mattering.
_IDLE_SLEEP_SECONDS = 5.0
# The failures that are the *operator's* to fix, and so the ones `main`
# answers with a message instead of a stack. Public because the boundary's
# whole design lives in what is and is not in this tuple, and a test asserts
# on it directly.
#
# **`Exception` is deliberately absent, and that is the decision rather than
# an oversight.** Catching it would also collapse every `AttributeError` and
# `TypeError` -- the bugs -- to one line, which trades a cosmetic wart for a
# lost bug report. So a family is added here only when an operator can act on
# it: start the database, fix the URL, reconnect the network.
#
# `OSError` rather than a SQLAlchemy type alone because asyncpg lets a refused
# TCP connection out **unwrapped** -- the exact failure M7's smoke test hit
# was a bare `ConnectionRefusedError`, and a handler keyed on
# `SQLAlchemyError` would have missed the one case this boundary exists for.
OPERATOR_ERRORS: tuple[type[Exception], ...] = (
    # A refused connection, a name that does not resolve, a full disk, a
    # bulk dataset that is not where it was left.
    OSError,
    # Everything the driver does wrap: a missing table (`alembic upgrade
    # head` never ran), a dead pool, a permission the role does not have.
    SQLAlchemyError,
    # TMDb, Emby, and every bulk download. An unreachable source is the same
    # class of operator problem as an unreachable database.
    httpx.HTTPError,
)
# Shorter than this and a rejected value is not a credential, and scrubbing
# it would mangle the message it appears in ("not 4" -> "not <redacted>").
_SHORTEST_REDACTABLE = 4
# 128 + SIGINT, the shell's convention, so a wrapping script can tell an
# operator's Ctrl-C from a command that failed.
_INTERRUPTED_EXIT_CODE = 130


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


async def _bootstrap(settings: Settings, phase: str) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    # One client for every dataset: connection reuse across the whole run, and
    # one place that owns closing it. Each adapter's `aclose` is deliberately
    # a no-op for exactly this reason -- closing a shared client from inside
    # one dataset would break its siblings.
    client = httpx.AsyncClient(timeout=60.0, headers={"User-Agent": settings.bulk_user_agent})
    try:
        async with factory() as session:
            catalog = PostgresBulkCatalogRepository(session)
            service = BootstrapService(
                PostgresImportRunRepository(session), catalog, session.commit
            )
            if phase in ("imdb", "all"):
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
            if phase in ("tmdb-ids", "all"):
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
            if phase in ("crosswalk", "all"):
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
            if phase in ("movielens", "all"):
                await _movielens(settings, client, catalog, service)
            logger.info("catalog now holds {count} titles", count=await catalog.count_titles())
    finally:
        await client.aclose()
        await engine.dispose()


async def _movielens(
    settings: Settings,
    client: httpx.AsyncClient,
    catalog: BulkCatalogRepository,
    service: BootstrapService,
) -> None:
    """The MovieLens tag genome, and the coverage report that is the actual
    deliverable of this phase.

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
    """
    if await catalog.count_titles() == 0:
        print(
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
    await service.import_dataset(dataset, write, revision=revision)
    _report_coverage(await catalog.genome_coverage(), _GENOME_TALLY["unmatched"])


# The `unmatched` count has nowhere else to go: `BootstrapService.import_dataset`
# takes a writer returning `int` (rows written) and knows nothing about a
# join's misses. A module-level tally rather than a wider port change, because
# a join's miss count is this one phase's report and not a property of every
# bulk import -- and the alternative, widening the writer's return type, would
# touch all four existing call sites for one caller's benefit.
_GENOME_TALLY = {"unmatched": 0}


def _percent(part: int, whole: int) -> str:
    return "n/a (0 titles)" if whole == 0 else f"{100.0 * part / whole:.2f}%"


def _report_coverage(coverage: GenomeCoverage, unmatched: int) -> None:
    """Four fractions, the enriched-tier one last because it is the one that
    matters.

    PRD 05 promised "~7% coverage" and PRD 04 repeated it as "~7% of the
    priority tier", and that figure has never had a denominator. Three of
    these are ceilings the *dataset* can reach; the fourth is what the join
    actually did against this operator's catalog.
    """
    print(f"movielens: {coverage.with_vector} vectors stored ({unmatched} unmatched)")
    print(f"  {_percent(coverage.with_vector, coverage.titles)} of {coverage.titles} titles")
    print(f"  {_percent(coverage.with_vector, coverage.movies)} of {coverage.movies} movies")
    print(
        f"  {_percent(coverage.enriched_with_vector, coverage.enriched)} of the enriched "
        f"tier ({coverage.enriched_with_vector} of {coverage.enriched} titles)"
    )
    # Only when there is more than one. A single-revision table is the normal
    # case and a line reading "revisions: 1" is noise; a table carrying two is
    # a correctness problem `GenomeRepository.get_pair` is already refusing to
    # blend across, and the fix is a re-import.
    if len(coverage.revisions) > 1:
        print("  MIXED RELEASES -- get_pair refuses to compare across these; re-import:")
        for name, count in coverage.revisions:
            print(f"    {name}: {count}")


async def _status(settings: Settings) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            runs = await PostgresImportRunRepository(session).list_runs()
            catalog = PostgresBulkCatalogRepository(session)
            catalog_size = await catalog.count_titles()
            # A phase whose deliverable is coverage has to be visible in the
            # command an operator runs to see coverage.
            genome = await catalog.genome_coverage()
    finally:
        await engine.dispose()
    # Printed, not logged: this is a report an operator asked for, and routing
    # it through the JSON log sink would make it unreadable at a terminal.
    print(f"titles in catalog: {catalog_size}")
    print(f"genome vectors: {genome.with_vector}")
    if not runs:
        print("no import has been run yet")
        return
    for run in runs:
        print(
            f"{run.dataset:<24} {run.status.value:<10} "
            f"position={run.position} seen={run.rows_seen} written={run.rows_written}"
            + (f" error={run.error}" if run.error else "")
        )


async def _open_adapter(pipeline: Pipeline, source: Source) -> SourceAdapter | None:
    """`composition.open_adapter`, with the operator told at a terminal.

    The wrapper exists for the *reporting*, not for the wiring: an operator
    who ran `usher sync` and got nothing needs the reason on stdout, and the
    shared helper logs it -- which is what the lane supervisor needs, since
    a lane has no terminal. `NO_CREDENTIALS` is one string so the two
    surfaces cannot drift into two explanations of one thing.
    """
    adapter = await open_adapter(pipeline, source)
    if adapter is None:
        print(f"{source.name}: {NO_CREDENTIALS}")
    return adapter


@asynccontextmanager
async def _session_for(settings: Settings) -> AsyncIterator[AsyncSession]:
    """One engine, one session, disposed however the command ends.

    Every command below is one process doing one thing, so a single session
    is the whole unit of work -- unlike `api/deps.py`, where the session is
    request-scoped and the engine outlives it on `app.state`.
    """
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _sync(
    settings: Settings, *, source_name: str | None, kind: str, allow_full_retraction: bool
) -> None:
    """Walk each selected source: items first, then watch state.

    The two lanes are one command because they are one operator intention
    ("bring this server up to date") and because the item walk has to run
    first -- `WatchStateSyncService` resolves each state against a
    `MediaItem`, so a watch lane that ran before the items existed would
    count every state unmatched and merge nothing.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(
            session, settings, max_retract_fraction=1.0 if allow_full_retraction else None
        )
        sources = await selected_sources(pipeline, source_name)
        if not sources:
            print("no enabled source matched" if source_name else "no enabled sources configured")
            return
        user_id = await ensure_default_user(session)
        await session.commit()
        for source in sources:
            adapter = await _open_adapter(pipeline, source)
            if adapter is None:
                continue
            try:
                # `aclose` in a `finally`, the rule `SourceService.status`
                # already documents: one adapter is one connection pool, and
                # a walk that raises would otherwise leak it for the rest of
                # the process.
                run = await pipeline.reconcile.reconcile(source, SyncRunKind(kind), adapter)
                print(
                    f"{source.name}: {run.kind.value} {run.status.value} "
                    f"seen={run.items_seen} matched={run.items_matched} "
                    f"unmatched={run.items_unmatched} retracted={run.items_retracted}"
                    + (f" error={run.error}" if run.error else "")
                )
                watch = await pipeline.watch.sync(source, adapter, user_id=user_id)
                print(
                    f"{source.name}: watch_state {watch.status.value} "
                    f"seen={watch.items_seen} merged={watch.items_matched} "
                    f"unmatched={watch.items_unmatched}"
                    + (f" error={watch.error}" if watch.error else "")
                )
            finally:
                await adapter.aclose()


async def _sync_status(settings: Settings) -> None:
    """Every source's recent runs, plus queue depth and parked count.

    Must work against an empty database: a command an operator can only run
    *after* a successful sync is no use for diagnosing why the sync did not
    happen.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        sources = await pipeline.sources.list_all()
        report: list[str] = []
        for source in sources:
            runs = await pipeline.runs.list_for_source(source.id, limit=5)
            if not runs:
                report.append(f"{source.name}: no sync has been run yet")
                continue
            for run in runs:
                report.append(
                    f"{source.name:<24} {run.kind.value:<12} {run.status.value:<10} "
                    f"seen={run.items_seen} matched={run.items_matched} "
                    f"unmatched={run.items_unmatched} retracted={run.items_retracted}"
                    + (f" error={run.error}" if run.error else "")
                )
        depth = await pipeline.queue.depth()
        parked = await pipeline.queue.parked(limit=1000)
    if not sources:
        print("no sources configured")
    for line in report:
        print(line)
    for job_kind in JobKind:
        print(f"queue {job_kind.value:<16} pending={depth[job_kind]}")
    print(f"parked jobs: {len(parked)}")
    for job in parked[:20]:
        print(f"  {job.kind.value:<16} {job.key} attempts={job.attempts} error={job.last_error}")


async def _unmatched(
    settings: Settings, *, limit: int, offset: int, resolve: str | None, title: str | None
) -> None:
    """The review queue (PRD 02: "unmatched items are never dropped").

    Listing and resolving are one command rather than two because they are
    one loop: an operator reads a page, resolves one line of it, and reads
    the next.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if resolve is not None and title is not None:
            attached = await pipeline.media_items.attach_title(
                _as_uuid(resolve, "media item id"),
                title_id=_as_uuid(title, "title id"),
                # `None`, deliberately: a hand resolution names a `Title`.
                # An episode-level resolution needs an `Episode.id` an
                # operator has no way to read off this listing, and M9's
                # route is where that grows a second argument.
                episode_id=None,
            )
            await session.commit()
            print("resolved" if attached else "no such media item")
            return
        items = await pipeline.media_items.list_unmatched(limit=limit, offset=offset)
    if not items:
        print("nothing unmatched")
        return
    for item in items:
        print(f"{item.id} {item.external_id:<40} added_at={item.added_at}")


async def _work(settings: Settings, *, once: bool) -> None:
    """Run queued jobs: `match`, `enrich`, `watch_history`, `index`, `derive`,
    `curate`.

    Owns the one `httpx.AsyncClient` behind `TmdbClient`, because the token
    bucket that keeps this deployment under TMDb's ~40 rps ceiling lives on
    the client. A client per job would give every job its own budget, which
    is a rate limiter that limits nothing.

    **Publishes to `NullEventPublisher`, and that is a stated consequence
    rather than an oversight.** `usher work` is a separate process and M5's
    bus is in-memory, so an enrichment finished here reaches no SSE client;
    a client that refetches still gets the enriched title, which is PRD 08's
    own degradation rather than breakage. The server process runs the same
    worker as a lane (`usher.api.lanes`) so PRD 03's read-through loop
    closes there, and `EventPublisher` is a port precisely so the fix for
    the split deployment is a second implementation rather than a branch.
    """
    async with _session_for(settings) as session:
        provider, aclose = await metadata_provider(settings)
        # Both built once, here, and closed in the same `finally`. A model is
        # a process-lifetime resource for the same reason the TMDb client is:
        # `build_worker` runs once per pass below, and a load there is 4.84 s
        # cold / 0.13 s warm over 65 MB of ONNX.
        model, aclose_model = await embedder(settings)
        # And the completion client, on the same terms: one per process, not
        # one per pass. `USHER_LLM_ENABLED=false` is the shipped default and
        # answers `(None, no-op)`, which is what leaves `curate` unclaimed
        # here rather than parked.
        client, aclose_client = await llm_client(settings)
        pipeline = build_pipeline(session, settings, provider=provider)
        registry = SourceRegistry(pipeline)
        gauges = QueueGauges()
        register_queue_gauges(gauges.read)
        # PRD 10's embedding backlog, refreshed on the same beat and for the
        # same reason: an OTel observable callback runs on the metric reader's
        # background thread and cannot await an asyncpg query. Refreshed even
        # when this process has no model -- a worker without one leaves index
        # jobs for one that has, and the backlog is the number that says so.
        backlog = SearchGauges()
        register_search_gauges(backlog.read)
        try:
            worker = build_worker(
                pipeline,
                settings,
                provider=provider,
                embedder=model,
                client=client,
                resolve=registry.resolve,
                user_id=await ensure_default_user(session),
            )
            # PRD 08: "startup requeues anything left in_progress". Before
            # the first claim, so a previous process's abandoned claims are
            # this one's work rather than nobody's.
            await worker.startup()
            ran = await worker.run_once()
            await gauges.refresh(pipeline.queue)
            await backlog.refresh(pipeline.embeddings, pipeline.neighbors, settings.embedding_model)
            print(f"{ran} jobs")
            while not once:
                if ran == 0:
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                ran = await worker.run_once()
                await gauges.refresh(pipeline.queue)
                await backlog.refresh(
                    pipeline.embeddings, pipeline.neighbors, settings.embedding_model
                )
        finally:
            await registry.aclose()
            await aclose()
            await aclose_model()
            await aclose_client()


async def _derive(settings: Settings, *, backfill: bool, limit: int, page_size: int) -> None:
    """Report derivation coverage, or re-derive people and credits inline.

    **The bare form only reads** -- five counts, no writes -- so it is safe on
    a production box while diagnosing something, which is the same bargain
    `usher index`'s bare form takes.

    **`--backfill` walks the cache inline rather than enqueueing, and that is
    the one place this command deliberately does not follow `index`.**
    `_index`'s backfill enqueues because the worker owns the model and a CLI
    that embedded would load 65 MB of ONNX in a process whose job is to print
    two numbers. Derivation needs none of that: no model, no network call, no
    rate limit -- it is a JSONB read and three writes. So the queue would buy
    ordering, retry and backoff for work that needs none of the three, and
    enqueueing over the enriched tier instead would write 2k-10k `jobs` rows,
    claim them one at a time, and issue one `get` per row to do what `iterate`
    does in a page-walk reading the same payloads in one pass.

    **The one-shot backfill exists because M7 arrives after a catalog is
    already enriched.** Those titles were enriched by M4/M5/M6, their payloads
    are in the cache, and *nothing will ever re-enrich them* -- so nothing
    will ever enqueue a `derive` job for them. The steady state is the job
    kind, enqueued alongside `index` after each enrichment commits.

    **On an empty database every line reads 0 and the command exits 0.** PRD
    08: *"every one of them has to work against an empty database"*. The
    arithmetic hazard here is the coverage ratio, which is why the report
    prints two counts and no percentage: `titles_with_credits /
    cached_payloads` is `0/0` on exactly the deployment that rule exists for.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if not backfill:
            print("provider: tmdb")
            print(f"cached payloads: {await pipeline.payloads.count('tmdb'):,}")
            print(f"titles with credits: {await pipeline.credits.count_titles_with_credits():,}")
            print(f"people: {await pipeline.people.count():,}")
            print(f"collections: {await pipeline.collections.count():,}")
            return

        # The provider is required for `to_derivation` -- a pure mapping, no
        # client and no request -- and its absence is the same degradation
        # `usher index` reports for a missing embedder: narrowed, not broken,
        # and said once rather than guessed at.
        provider, aclose = await metadata_provider(settings)
        if provider is None:
            print("no TMDb API key configured; nothing can be derived from the cache")
            return
        try:
            report = await build_derive_service(pipeline, provider).derive_all(
                page_size=page_size, limit=limit
            )
        finally:
            await aclose()
        print(f"payloads read: {report.payloads_read:,}")
        print(f"titles derived: {report.titles_derived:,}")
        print(f"people written: {report.people_written:,}")
        print(f"credits written: {report.credits_written:,}")
        print(f"collections linked: {report.collections_written:,}")


async def _index(settings: Settings, *, backfill: bool, limit: int, page_size: int) -> None:
    """Report the search index's freshness, or enqueue the work that fixes it.

    **The bare form only reads**, so it is safe on a production box while
    diagnosing something. `--backfill` is the writing form and it is one
    `enqueue` per stale title, never an inline embed: the worker owns the
    model (`composition.embedder`), and a CLI that embedded would load 65 MB
    of ONNX in a process whose job is to print two numbers.

    **The model is not loaded here at all**, and `settings.embedding_model`
    below is what says so: staleness is a question about a *name*, which is
    exactly what recording `model_name` on the row bought. This command works
    on a deployment that has no embedding extra installed -- it will report
    what is stale and enqueue it for a worker that does.

    **Sized in tokens, because throughput is linear in tokens and not in
    texts.** CPU holds ~8,000-10,700 tokens/s across the whole range and a
    realistic `name + overview + genres + keywords` document is ~100-130
    tokens, so the enriched tier boundary call 4 embeds (2k-10k titles) is
    ~25 seconds to 2 minutes of worker time. Over all 1,271,138 titles it
    would be 4-6 hours, which is the number that boundary call avoids paying.
    A rate in texts/s would hide that a document twice as long costs twice as
    much.

    **Re-running is free.** `enqueue`'s upsert carries `WHERE jobs.status <>
    'parked' AND jobs.priority < excluded.priority`, so a second sweep over
    jobs already at BACKFILL costs one index probe per row and writes nothing.
    The reported count is rows *written*, which is the honest number and is 0
    on a second run.
    """
    gauges = SearchGauges()
    # Registered even in the bare read form, so the two numbers this prints and
    # the two PRD 10 exports are the same read rather than two reads that agree
    # today. A short-lived command exports on shutdown when an OTLP endpoint is
    # configured and does nothing when one is not, which is the same bargain
    # `register_queue_gauges` takes in `usher work`.
    register_search_gauges(gauges.read)
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        model = settings.embedding_model
        if not backfill:
            await gauges.refresh(pipeline.embeddings, pipeline.neighbors, model)
            snapshot = gauges.read()
            print(f"model: {model}")
            print(f"stale embeddings: {snapshot.stale}")
            print(f"refused (no content to embed): {snapshot.refused}")
            # ~135 tokens a document at ~8,000-10,700 tokens/s on CPU. A range
            # derived from the invariant rather than from a texts/s rate.
            #
            # **135 and not the ~115 M6 measured**, because M7's weight class
            # B added a seventh segment: `credit_names` holds up to ten names
            # at ~2 tokens each, so a credited document is ~20 tokens longer.
            # The 100-130 range in this function's docstring was measured for
            # a `name + overview + genres + keywords` document and is left
            # standing as what it is -- a measurement of a different document
            # shape -- rather than quietly restated for this one. Uncredited
            # titles, which are most of the catalog, still sit inside it; the
            # estimate is deliberately the pessimistic end, because an
            # operator reading it is deciding whether to start a backfill now.
            print(
                f"estimated worker time: {snapshot.stale * 135 / 10700:.0f}-"
                f"{snapshot.stale * 135 / 8000:.0f}s"
            )
            return

        written = seen = 0
        after: uuid.UUID | None = None
        while True:
            # Task 9's cursor, imported rather than re-derived. The predicate
            # it walks is the one `count_stale` above and the
            # `usher.search.embeddings.stale` gauge evaluate -- a backfill
            # with its own copy of a staleness rule is how a sweep and the
            # dashboard that reports on it come to disagree about what they
            # are counting.
            page = await pipeline.embeddings.list_stale(model, limit=page_size, after=after)
            if not page:
                break
            written += await pipeline.queue.enqueue(
                [
                    JobRequest(kind=JobKind.INDEX, key=str(title.id), priority=JobPriority.BACKFILL)
                    for title in page
                ]
            )
            await session.commit()
            seen += len(page)
            # **The cursor advances on the last id of the page, always** --
            # never on "how many were still stale afterwards". A loop that
            # re-asked the predicate would not terminate against a row the
            # predicate cannot clear, and this repository has shipped exactly
            # that non-convergence once, in the watch-history repair. A keyset
            # cursor cannot loop, because each pass starts strictly after the
            # last id it saw, whatever the predicate did.
            after = page[-1].id
            if limit and seen >= limit:
                break
        # After the sweep, not inside it. The predicate is cheap to count and
        # cheaper still not to count per page, and the number an operator wants
        # is the backlog *left over* -- the same reason `QueueGauges` refreshes
        # after a worker pass rather than before it.
        await gauges.refresh(pipeline.embeddings, pipeline.neighbors, model)
        print(f"{seen} stale titles swept, {written} index jobs written")


def _filters_from(args: argparse.Namespace) -> SearchFilters:
    """`SearchFilters`' whole closed vocabulary, built in one place.

    One function rather than a construction inlined at the call site, so the
    flag-to-field mapping exists once and
    `test_the_filter_flags_are_search_filters_whole_vocabulary` has something
    to check it against. **All six, not the useful ones**: 🔶 1's settlement
    made the vocabulary closed precisely because a `dict[str, Any]` let two
    backends invent different keys, and a filter with no flag is a capability
    the port declares, the backend implements, and no operator can reach.

    Empty tuples rather than `None` for the two list-shaped filters: the port
    reads `()` as "narrow nothing", and `argparse`'s `action="append"` default
    is `None`, so the conversion has to happen somewhere and here is the only
    place it can happen once.
    """
    return SearchFilters(
        kinds=tuple(TitleKind(kind) for kind in args.kinds or ()),
        year_from=args.year_from,
        year_to=args.year_to,
        genres=tuple(args.genres or ()),
        owned_only=args.owned_only,
        min_enrichment=(
            None if args.min_enrichment is None else EnrichmentState(args.min_enrichment)
        ),
    )


async def _search(
    settings: Settings, *, query: str, mode: str, limit: int, filters: SearchFilters
) -> None:
    """PRD 05's search, at a terminal.

    **Reports coverage on every run, which is the point of this command having
    a human-readable mode at all.** A `FUSED` search against a catalog with no
    embeddings degrades to full-text -- correctly, because a title with no
    vector is *absent from the semantic candidate list* rather than ranked
    last -- and the result looks exactly like a working hybrid search. No
    error, no empty result, no log line. This milestone's headline failure
    mode, arriving at the CLI.

    **Two different problems present identically and get different sentences**,
    which is what `SearchAnswer` carrying `requested_mode` beside `mode` is
    for. `degraded` means the deployment has no model at all and the fix is an
    extra plus a setting; `semantic_coverage == 0.0` on an undegraded FUSED
    search means the model is there and nothing has been embedded yet, and the
    fix is `usher index --backfill`. A single warning for both would send an
    operator to the wrong one half the time.

    **The embedder is built here and closed in the same `finally`**, and only
    when a non-full-text mode asks for one. It is a once-per-process resource
    (`composition.embedder`), which for a command is once; `build_pipeline`
    deliberately never builds one, so a full-text search costs no model load
    at all. `SearchRequest.__post_init__` refuses a `SEMANTIC` or `FUSED`
    request with no vector, so the only object that can construct one is the
    object holding the model -- which is why this passes primitives to
    `SearchService.search` and never a `SearchRequest`.
    """
    requested = SearchMode(mode)
    model, aclose_model = (
        # `report=False`: that factory's warning is about a *lane* ("index jobs
        # will not be claimed"), which is right for `usher work` and wrong
        # twice over here -- it advises about work this process does not do,
        # and `cli.py`'s printed-not-logged rule makes it a JSON envelope in
        # front of the results. The line printed below says the same thing
        # better, naming the setting and the extra.
        await embedder(settings, report=False)
        if requested is not SearchMode.FULL_TEXT
        else (None, nothing)
    )
    try:
        async with _session_for(settings) as session:
            pipeline = build_pipeline(session, settings, embedder=model)
            try:
                answer = await pipeline.search.search(
                    query, mode=requested, limit=limit, filters=filters
                )
            except SemanticSearchUnavailable as exc:
                # Not narrowed to full-text, and the service is right to refuse
                # rather than answer: the caller asked the one question
                # full-text cannot answer and would otherwise get a plausible
                # answer to a different one. `SystemExit` with a sentence, the
                # treatment `_as_uuid` gives a bad id.
                raise SystemExit(f"{exc} -- try --mode fused, or run `usher index`") from exc
    finally:
        await aclose_model()

    for rank, result in enumerate(answer.results, start=1):
        year = f" ({result.year})" if result.year else ""
        owned = "*" if result.owned else " "
        print(f"{rank:>3} {owned} {result.score:6.4f}  {result.name}{year}  {result.title_id}")
    if not answer.results:
        print("no match")
    # Always, not only when it is low: a number an operator sees only when
    # something is wrong is a number they have no baseline for.
    print(
        f"mode={answer.mode.value} results={len(answer.results)} "
        f"semantic_coverage={answer.semantic_coverage:.3f}"
    )
    if answer.degraded:
        print(
            f"warning: {answer.requested_mode.value} was served as {answer.mode.value} -- "
            "this deployment has no embedding model "
            "(set USHER_EMBEDDING_ENABLED=true and install the `embedding` extra)"
        )
    elif answer.mode is SearchMode.FUSED and answer.semantic_coverage == 0.0:
        # The warning names the command that fixes it, which is the difference
        # between a diagnostic and a complaint.
        print(
            "warning: no title in the filtered population has an embedding, so this "
            "was full-text only -- run `usher index --backfill`"
        )


async def _suggest(settings: Settings, *, prefix: str, limit: int) -> None:
    """Type-ahead, at a terminal.

    **No embedder in either direction.** `SuggestIndex` is its own port
    (🔶 2) and `PostgresSuggestIndex` queries `titles` through a trigram index
    and writes nothing, so this command starts in 0.13 s on any deployment --
    including one with no embedding extra installed at all, which is PRD 05's
    catalog-lookup tier serving all 1.27M titles with no model.

    No coverage line, and that is not an omission: there is no semantic lane
    here to have degraded.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        results = await pipeline.search.suggest(prefix, limit=limit)
    for result in results:
        year = f" ({result.year})" if result.year else ""
        print(f"{result.score:6.4f}  {result.name}{year}  {result.title_id}")
    if not results:
        print("no match")


async def _similar(
    settings: Settings, *, title_id: uuid.UUID | None, limit: int, rebuild: bool
) -> None:
    """Read one title's precomputed neighbours, or recompute the whole table.

    **No model is loaded in either form**, and that is a property of the
    design rather than an optimisation: the rebuild reads stored vectors and
    never embeds anything, so this command starts in 0.13 s instead of paying
    a 4.84 s cold ONNX load. A deployment with no embedding extra installed can
    still rebuild neighbours over whatever a worker elsewhere indexed.

    **`--rebuild` is not a job kind, and the argument is about the unit of
    work.** Re-embedding one title changes the neighbour lists of every title
    it is near, and no per-seed job can know which those are without doing the
    whole computation anyway -- so a `JobKind.SIMILAR` keyed on a title id
    would update the seed's own row and leave every list that should now
    contain it untouched, producing a table that is never coherent and whose
    incoherence is invisible from any single row.

    **And the cost of that decision, stated rather than hidden: nothing in M6
    re-runs this.** It is an operator's command or a cron entry, run after
    `usher index --backfill`. PRD 06's "TTL: hours" is a statement about how
    long M7 may cache what it read, not a promise that this table is hours
    fresh.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if rebuild:
            report = await pipeline.similar.rebuild()
            print(f"rebuilt {report.seeds} seeds, wrote {report.rows} neighbour rows")
            # **The genome's coverage, with its denominators, printed by the
            # thing that consumed the vectors.** PRD 05 promised "~7%" since
            # before an importer existed and never said of what; these are the
            # two numbers that answer it, and the second is the one that
            # decides whether the term can promote anything.
            #
            # The pair rate is *measured*, never squared: genome membership
            # and candidate-pool membership both correlate with popularity and
            # with enrichment, so `coverage ** 2` is wrong in an unknown
            # direction.
            if report.seeds:
                share = 100.0 * report.seeds_with_genome / report.seeds
                print(
                    f"{report.seeds_with_genome} of {report.seeds} seeds carried a genome "
                    f"vector ({share:.2f}%)"
                )
            if report.candidate_pairs:
                pair_share = 100.0 * report.pairs_with_tags / report.candidate_pairs
                print(
                    f"{report.pairs_with_tags} of {report.candidate_pairs} candidate pairs "
                    f"scored a genome cosine ({pair_share:.2f}%)"
                )
            if report.without_embedding:
                # Excluded *and* counted. A rebuild that silently skipped a
                # growing swathe of the catalog reads exactly like one with
                # nothing to skip, which is this milestone's own failure mode.
                print(
                    f"{report.without_embedding} titles have no embedding and were excluded "
                    "-- run `usher index --backfill` if that is unexpected"
                )
            return

        if title_id is None:  # pragma: no cover - `parse_args` refuses this
            raise SystemExit("give a title id, or --rebuild, but not both")
        rows = await pipeline.similar.neighbors_of(title_id, limit=limit)
        for row in rows:
            year = f" ({row.year})" if row.year else ""
            print(f"{row.score:.3f}  {row.name}{year}  {row.title_id}")
        # **Narrowed, not broken** -- PRD 08's degradation rule, the same shape
        # `--mode fused` takes when it cannot reach the semantic lane. The
        # neighbours still print: they are internally consistent and perfectly
        # readable, they were simply computed under a different blend, and
        # refusing to show them would turn "out of date" into "regressed".
        if rows and await pipeline.similar.stale_neighbors(title_id=title_id):
            print(
                "these neighbours were computed under a different blend; "
                "run `usher similar --rebuild`"
            )
        if not rows and await pipeline.similar.computed_at() is None:
            # Two causes for an empty answer and only one is a fact about the
            # title. One message for both sends an operator to look at the
            # wrong thing.
            print("no neighbours have ever been computed -- run `usher similar --rebuild`")
        elif not rows:
            print("no neighbours for this title")


async def _home(settings: Settings, *, limit: int, repeat: int) -> None:
    """Compose the home screen, and time it.

    **Ships alongside `GET /home` rather than instead of it**, which is the
    reverse of `usher search` and `usher similar`. ADR-0006's claim -- one
    request paints a screen -- is a property of a request boundary that no
    command can exhibit, so there the route is the deliverable. What this
    command is for is PRD 08's rule that every operator command works against
    an empty database, and the arithmetic that rule is hunting: **the taste
    centroid is a mean, and the mean of zero embeddings is 0/0.**

    **And it is where boundary call 8's promise is kept.** The rows build
    sequentially because `AsyncSession` is not safe for concurrent use; whether
    that is *fast enough* is a measurement rather than an argument, and this is
    the measurement. Revisit the sequential build when
    `usher.home.compose.duration` p95 exceeds **400 ms** *and* no single
    provider accounts for **50%** or more of the total build time -- over
    budget with a dominant provider is a query to fix, not a build to
    parallelise, and under budget is neither. If both hold, the redesign is a
    session per row behind a bounded pool, i.e. a lane, and PRD 01's
    concurrency table grows the row boundary call 8 says it does not have.
    Both numbers are printed, so the rule is read off the output rather than
    recomputed.

    **Every registered provider gets a line, including the ones that proposed
    nothing.** An absent provider and a silent one are the two states this
    milestone exists to distinguish, so the report iterates the *registry* and
    never the proposals -- `HomeService.compose_report` is what makes that
    possible without a second loop describing a composition that never
    happened.

    **`--repeat` measures N *cold* compositions**, clearing the cache before
    each. A repeat that measured cache hits would report a number near zero and
    mean nothing. The warm read is timed once, separately, and labelled -- and
    it is the only measurement of the cache this milestone has, because
    `usher.cache.hits`/`.misses` is M9's.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        user = await default_user(session)
        # The same wiring `api/deps.py` builds per request, minus the request:
        # `taste` and `affinities` are values the composer hands over, because
        # a provider may import only `domain/` and `ports/`.
        ctx = RowContext(
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
            affinities=await pipeline.taste.genre_affinity(user.id),
            curated=pipeline.curated_rows,
        )
        cache = RowCache(clock=lambda: datetime.now(UTC))
        service = HomeService(pipeline.row_providers, cache=cache, max_rows=limit)

        # Collected rather than overwritten so the last one is reachable
        # without an `Optional` no input can reach -- `parse_args` refuses
        # `--repeat 0`, and `assert` is not available in shipped code.
        reports: list[ComposeReport] = []
        for _ in range(repeat):
            # Cleared *before* each run, so every one of them is cold. Without
            # this the second run is a cache hit and the measurement silently
            # becomes a benchmark of a dict.
            cache.clear()
            reports.append(await service.compose_report(ctx))
        report = reports[-1]
        cold = [one.duration_seconds for one in reports]

        warm_at = time.perf_counter()
        await service.compose(ctx)
        warm = time.perf_counter() - warm_at

        _print_home_report(report, cold=cold, warm=warm)


def _print_home_report(report: ComposeReport, *, cold: Sequence[float], warm: float) -> None:
    """The operator's table. `print`, never `logger` -- the split every command
    in this module makes: loguru output is operational and goes to a sink an
    operator may not be reading, and a command's answer is stdout, which is
    what gets piped."""
    print(f"{'provider':<22}{'proposed':>9}{'built':>7}{'cards':>7}{'propose':>11}{'build':>11}")
    for one in sorted(report.providers, key=lambda entry: entry.provider):
        built = "-" if one.selected == 0 else str(one.built)
        cards = "-" if one.selected == 0 else str(one.cards)
        build = "-" if one.selected == 0 else f"{one.build_seconds * 1000:.1f} ms"
        print(
            f"{one.provider:<22}{one.proposed:>9}{built:>7}{cards:>7}"
            f"{one.propose_seconds * 1000:>8.1f} ms{build:>11}"
        )
    print()
    print(
        f"{len(report.providers)} providers, {report.silent} proposed nothing, "
        f"{report.dropped} built empty and was dropped"
    )
    print(f"screen: {len(report.rows)} rows, {report.cards} cards")
    ordered = sorted(cold)
    p50 = ordered[len(ordered) // 2]
    # The p95 of one sample is that sample, which is honest rather than
    # flattering -- and `--repeat` is how an operator buys a real one.
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(
        f"compose (cold)  p50 {p50 * 1000:.1f} ms  p95 {p95 * 1000:.1f} ms "
        f"over {len(cold)} run(s)     compose (warm, from cache)  {warm * 1000:.1f} ms"
    )
    # **The second half of boundary call 8's rule, computed rather than left to
    # the reader.** If one provider is most of the wall clock, parallelising
    # nine of them converges on that provider's latency and buys nothing -- the
    # finding is a query to fix.
    total_build = sum(one.build_seconds for one in report.providers)
    if total_build > 0:
        slowest = max(report.providers, key=lambda entry: entry.build_seconds)
        share = slowest.build_seconds / total_build
        print(
            f"slowest provider: {slowest.provider} at {slowest.build_seconds * 1000:.1f} ms "
            f"({share:.0%} of build time)"
        )
    else:
        print("nothing was built, so there is no build time to attribute")
    # **Printed unconditionally**, and that is a correction the empty-database
    # case caught: guarded on `total_build > 0` the rule an operator needs is
    # missing from exactly the run where they most need to know what the
    # numbers mean, which is the one against a household that has watched
    # nothing.
    print(
        "revisit the sequential build only when p95 > 400 ms AND no single provider "
        "is >= 50% of build time"
    )


async def _push(settings: Settings, *, source_name: str | None, probe: bool) -> None:
    """Probe a source's push channel once, or run the lanes in the foreground.

    `--probe` is the operator-facing form of ADR-0004's caveat: it reports
    the **messages and events that arrived**, never that the handshake
    succeeded, because a handshake against a nonexistent path also upgrades
    and also receives `Sessions`. It is the one thing in this project that
    opens a socket on purpose to answer a question, which is why `verify()`
    does not have to.

    Bare `usher push` runs exactly the lanes `create_app` would, honouring
    `USHER_PUSH_ENABLED`/`USHER_WORKER_ENABLED`, with no HTTP server -- the
    other side of PRD 01's "`--worker` entrypoint flag ... so lanes can be
    moved to a separate container later by editing compose". It publishes to
    a `NullEventPublisher` for the reason `usher work` does: the bus is
    in-memory and there is no SSE client in this process.
    """
    if not probe:
        await _run_lanes(settings)
        return
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        sources = await selected_sources(pipeline, source_name)
        if not sources:
            print("no enabled source matched" if source_name else "no enabled sources configured")
            return
        for source in sources:
            adapter = await _open_adapter(pipeline, source)
            if adapter is None:
                continue
            try:
                result = await adapter.probe_push(timeout_seconds=settings.push_stale_after_seconds)
                print(
                    f"{source.name}: upgraded={result.upgraded} "
                    f"delivering={result.delivering} "
                    f"events={[kind.value for kind in result.events] or 'none'}"
                    + (f" detail={result.detail}" if result.detail else "")
                )
            finally:
                await adapter.aclose()


async def _run_lanes(settings: Settings) -> None:
    """`create_app`'s lanes, with no app around them.

    The engine and the session factory are built here rather than by the
    supervisor, for the same reason the lifespan builds them: a lane holds
    one unit of work at a time and the engine outlives all of them. Stops on
    Ctrl-C -- `KeyboardInterrupt` reaches `asyncio.run`, which cancels the
    task, and `stop()` runs in the `finally`.
    """
    engine = build_engine(settings.database_url.get_secret_value())
    sessions = build_session_factory(engine)
    provider, close_provider = (
        await metadata_provider(settings) if settings.worker_enabled else (None, nothing)
    )
    model, close_model = await embedder(settings) if settings.worker_enabled else (None, nothing)
    events = NullEventPublisher()
    lanes = LaneSupervisor(
        settings,
        unit_of_work(sessions, settings, events=events, provider=provider),
        events,
        user_id=DefaultUserId(sessions),
        provider=provider,
        embedder=model,
    )
    await lanes.start()
    try:
        # Nothing to serve, so the process is the lanes. `asyncio.Event()`
        # that nothing sets rather than a sleep loop: it costs no wakeups
        # and it cancels cleanly.
        await asyncio.Event().wait()
    finally:
        await lanes.stop()
        await close_provider()
        await close_model()
        await engine.dispose()


def _as_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SystemExit(f"{what} is not a uuid: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usher")
    # The error boundary's own escape hatch, and the reason the boundary is
    # allowed to swallow a stack at all. Top-level rather than per-command
    # (`usher --traceback bootstrap-status`) because the boundary is
    # top-level; it is **not** a `Settings` field, since a knob that turns
    # off a presentation choice for one invocation is not deployment
    # configuration.
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="show the full stack instead of a one-line message",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (the default with no arguments)")
    bootstrap = sub.add_parser("bootstrap", help="import bulk catalog datasets")
    bootstrap.add_argument("--phase", choices=PHASES, default="all")
    sub.add_parser("bootstrap-status", help="report import progress and catalog size")

    sync = sub.add_parser("sync", help="walk a source into the catalog")
    sync.add_argument("--source", default=None, help="source name; omit for every enabled source")
    sync.add_argument("--kind", choices=SYNC_KINDS, default="full")
    sync.add_argument(
        "--allow-full-retraction",
        action="store_true",
        help=(
            "let this run mark the whole source unavailable "
            "(ADR-0015; only for a library the operator really did remove)"
        ),
    )
    sub.add_parser("sync-status", help="report recent sync runs, queue depth, and parked jobs")

    unmatched = sub.add_parser("unmatched", help="list or resolve the review queue")
    unmatched.add_argument("--limit", type=int, default=50)
    unmatched.add_argument("--offset", type=int, default=0)
    # A pair, not two independent flags: `attach_title` writes what it is
    # given, so `--resolve` alone would blank a link rather than create one.
    resolve = unmatched.add_argument_group("resolve one item")
    resolve.add_argument("--resolve", default=None, help="media item id to attach")
    resolve.add_argument("--title", default=None, help="title id to attach it to")

    work = sub.add_parser("work", help="run queued jobs")
    work.add_argument("--once", action="store_true", help="one pass, then exit")

    index = sub.add_parser("index", help="report search-index freshness, or enqueue the work")
    index.add_argument(
        "--backfill",
        action="store_true",
        help="enqueue one index job per stale title (the bare form only reads)",
    )
    index.add_argument("--limit", type=int, default=0, help="stop after N titles; 0 drains")
    index.add_argument("--page-size", type=int, default=1000)

    derive = sub.add_parser(
        "derive", help="report derivation coverage, or re-derive from the cache"
    )
    derive.add_argument(
        "--backfill",
        action="store_true",
        help="walk the cached payloads and re-derive inline (the bare form only reads)",
    )
    derive.add_argument("--limit", type=int, default=0, help="stop after N payloads; 0 drains")
    # 500 rather than `index`'s 1000: a page here carries whole JSONB payloads
    # rather than title ids, and 500 TMDb detail responses at ~8 kB is ~4 MB in
    # flight. A number to keep in mind, not a measured optimum.
    derive.add_argument("--page-size", type=int, default=500)

    search = sub.add_parser("search", help="search the catalog")
    search.add_argument("query", help="what to search for")
    # `SearchMode`'s values, taken from the enum rather than retyped: a
    # hand-copied list drifts silently and offers an operator a mode the
    # service cannot serve -- or, worse, omits the one ADR-0002's whole design
    # is about.
    search.add_argument(
        "--mode", choices=[mode.value for mode in SearchMode], default=SearchMode.FUSED.value
    )
    search.add_argument("--limit", type=int, default=20)
    # `SearchFilters`' closed vocabulary, one flag per field and no more. The
    # vocabulary being closed is 🔶 1's settlement -- a `dict[str, Any]` let
    # two backends invent different keys, and a backend that cannot express a
    # filter must raise rather than ignore it, because an ignored filter
    # returns *more* results and reads as working. So this is not "the useful
    # ones"; it is all of them, and a new filter is a port change before it is
    # a flag.
    search.add_argument(
        "--kind", action="append", dest="kinds", choices=[kind.value for kind in TitleKind]
    )
    search.add_argument("--year-from", type=int, default=None)
    search.add_argument("--year-to", type=int, default=None)
    search.add_argument("--genre", action="append", dest="genres")
    search.add_argument("--owned-only", action="store_true")
    search.add_argument(
        "--min-enrichment",
        choices=[state.value for state in EnrichmentState],
        default=None,
    )

    suggest = sub.add_parser("suggest", help="type-ahead over titles")
    suggest.add_argument("prefix")
    suggest.add_argument("--limit", type=int, default=10)

    similar = sub.add_parser("similar", help="titles like this one, or rebuild the table")
    # Optional because `--rebuild` is the write form of the same command. Two
    # subcommands for one artefact is how `usher index` and its backfill would
    # have drifted; the cross-argument rule in `parse_args` is what argparse
    # cannot express.
    similar.add_argument("title_id", nargs="?")
    similar.add_argument("--limit", type=int, default=10)
    similar.add_argument(
        "--rebuild",
        action="store_true",
        help="recompute title_neighbors for the whole embedded population",
    )

    home = sub.add_parser("home", help="compose the home screen, and time it")
    home.add_argument("--limit", type=int, default=10, help="rows to compose")
    home.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="cold compositions to time; the cache is cleared before each",
    )

    push = sub.add_parser("push", help="run the push lane, or probe a source's push channel")
    push.add_argument("--source", default=None, help="source name; omit for every enabled source")
    push.add_argument(
        "--probe",
        action="store_true",
        help="connect, wait, and report what arrived, then exit",
    )
    return parser


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """`build_parser().parse_args`, plus the cross-argument rules argparse
    has no vocabulary for.

    A separate function rather than a `build_parser` that validates, so the
    parser stays a pure description of the surface -- and a *public* one
    rather than a private step inside `main`, because the rule below is the
    only thing standing between `--resolve <id>` with no `--title` and an
    `attach_title(title_id=None)` that blanks a link instead of creating
    one, and a rule with no reachable test is a comment.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv))
    if args.command == "unmatched" and (args.resolve is None) != (args.title is None):
        # `parser.error`, not a raise: it exits 2 with usage on stderr, the
        # same way every other argument failure does.
        parser.error("--resolve and --title are used together")
    if args.command == "search":
        if (
            args.year_from is not None
            and args.year_to is not None
            and args.year_from > args.year_to
        ):
            # An empty range is not something argparse can see: each bound is
            # individually valid, so a transposed pair parses cleanly and then
            # returns nothing -- which reads as "the catalog does not have it".
            parser.error("--year-from must not be after --year-to")
        if args.limit < 1:
            # Here rather than left to `SearchService`'s ceiling, because the
            # two failures differ: above `search_result_limit` the service
            # clamps and the answer says so, and at zero the operator asked for
            # nothing and meant something.
            parser.error("--limit must be at least 1")
    if args.command == "suggest" and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.command == "home":
        # **No cross-argument rule, and that is stated rather than omitted.**
        # `usher similar` needs one because its two arguments select between
        # two *different operations*, one of which rewrites a whole table.
        # `home` has one operation and two scalars, so only the bounds matter.
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        if args.repeat < 1:
            parser.error("--repeat must be at least 1")
    if args.command == "similar" and bool(args.title_id) == bool(args.rebuild):
        # Both spellings refused: no arguments is a read of nothing, and both
        # together is a read and a write in one command. `parser.error` again
        # -- exit 2 with usage rather than exit 1 with a traceback.
        parser.error("give a title id, or --rebuild, but not both")
    return args


def _command_name(args: argparse.Namespace) -> str:
    """What to call the thing that failed, in a message an operator reads.

    `None` rather than `"serve"` is what argparse leaves behind when no
    subcommand was given, and `main` treats that as `serve` -- so the
    message has to as well, or the one command the container actually runs
    reports itself as `usher None`.
    """
    return args.command or "serve"


def _operator_problem(command: str, exc: BaseException) -> str:
    """One line for the failure, one for the way back to the stack.

    The type name is kept because `str(OSError)` on its own is
    `[Errno 111] Connect call failed ('db', 5432)` -- which says *where* and
    never *what*, and reads as a puzzle rather than as "the database is not
    up".
    """
    return (
        f"usher {command}: {type(exc).__name__}: {exc}\n"
        f"(the stack is one flag away: `usher --traceback {command}`)"
    )


def _settings_problem(command: str, exc: ValidationError) -> str:
    """pydantic's diagnosis with every rejected value stripped out.

    **This is a security control, not formatting.** A pydantic v2
    `ValidationError` renders as

        ... [type=value_error, input_value='mysql://admin:hunter2@db/usher', ...]

    so `USHER_DATABASE_URL` with the wrong driver printed the whole DSN, and
    a truncated `USHER_SECRET_KEY` printed the key. Both fields are
    `SecretStr` in `Settings` for exactly that reason; this CLI was the one
    reader that unwrapped them, and it did it on the surface an operator is
    most likely to paste into an issue.

    Same control, and same trade, as `usher.api.errors` makes for a 422:
    `loc` and `msg` survive -- so the operator still learns which setting was
    wrong and what it should have been -- and the value never does.

    `msg` is scrubbed as well as `input` dropped. No validator in `Settings`
    interpolates the value into its own message today, and none of pydantic's
    built-in messages do either; the scrub is there so that writing one does
    not quietly reopen this.
    """
    lines = [f"usher {command}: the settings were rejected"]
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(settings)"
        message = error["msg"]
        rejected = str(error.get("input", ""))
        if len(rejected) >= _SHORTEST_REDACTABLE and rejected in message:
            message = message.replace(rejected, "<redacted>")
        lines.append(f"  {where}: {message}")
    lines.append("(values are not shown -- any setting may be a credential)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    """Every entry point's single door: `python -m usher`, the `usher`
    console script (`[project.scripts]`), and the container's `CMD`.

    **`argv is None` means "read `sys.argv`", not "no arguments".** A
    console script is called as `main()` with nothing passed, so a `None`
    that fell through to the no-arguments branch made `usher sync-status`
    silently start the HTTP server -- an entry point that ignores everything
    it is given and looks like it works, because the server does start.
    `tests/unit/test_main.py` pins both halves.

    `argv or ["serve"]` after that: no arguments *at all* must keep starting
    the server, because that is exactly what the container's CMD runs
    (`alembic upgrade head && exec python -m usher`). Adding subcommands
    must not change it, and neither must adding an entry point.

    **The `try` is the whole error boundary for the CLI, and it is one
    `try` deliberately.** M7's smoke test found `bootstrap-status` and
    `sync-status` answering an unreachable database with sixty lines of
    asyncpg and greenlet frames; the operator's actual information was the
    last line. Per-command handling is the shape that rots -- the next
    command is written by copying an arm, not the handler -- so the
    boundary wraps `_dispatch` rather than living inside it, and
    `tests/unit/test_cli_errors.py` asserts that shape by AST as well as
    asserting the behaviour.

    Reading the settings is inside it too. A `.env` that fails validation is
    the same kind of failure as a database that is down, it reaches the
    operator through the same command, and it is the case that was leaking a
    credential (see `_settings_problem`).

    `SystemExit` is untouched by all of it: it is a `BaseException`, the
    handlers below name only `Exception` subclasses, and three places in
    this module already exit with a message chosen for the failure it
    describes.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(list(argv) if argv else ["serve"])
    try:
        settings = get_settings()
        configure_telemetry(settings)
        _dispatch(args, settings)
    except ValidationError as exc:
        # Before `OPERATOR_ERRORS` and, unlike it, **not reopened by
        # `--traceback`**: a settings failure's stack is always the same six
        # pydantic frames and diagnoses nothing, so the only thing re-raising
        # would add is the value `_settings_problem` exists to withhold.
        raise SystemExit(_settings_problem(_command_name(args), exc)) from exc
    except KeyboardInterrupt:
        # `usher bootstrap` is a multi-hour download an operator is *expected*
        # to interrupt. A `KeyboardInterrupt` traceback through `asyncio.run`
        # reads as the run failing rather than as their own decision.
        print("interrupted", file=sys.stderr)
        raise SystemExit(_INTERRUPTED_EXIT_CODE) from None
    except OPERATOR_ERRORS as exc:
        if args.traceback:
            # Bare `raise`, not `raise exc`: rebinding would replace the
            # stack the flag was asked for with this frame.
            raise
        raise SystemExit(_operator_problem(_command_name(args), exc)) from exc


def _dispatch(args: argparse.Namespace, settings: Settings) -> None:
    """The command table, lifted out of `main` so the boundary there is one
    `try` around all of it rather than one per arm."""
    if args.command == "bootstrap":
        asyncio.run(_bootstrap(settings, args.phase))
    elif args.command == "bootstrap-status":
        asyncio.run(_status(settings))
    elif args.command == "sync":
        asyncio.run(
            _sync(
                settings,
                source_name=args.source,
                kind=args.kind,
                allow_full_retraction=args.allow_full_retraction,
            )
        )
    elif args.command == "sync-status":
        asyncio.run(_sync_status(settings))
    elif args.command == "unmatched":
        asyncio.run(
            _unmatched(
                settings,
                limit=args.limit,
                offset=args.offset,
                resolve=args.resolve,
                title=args.title,
            )
        )
    elif args.command == "work":
        asyncio.run(_work(settings, once=args.once))
    elif args.command == "index":
        asyncio.run(
            _index(settings, backfill=args.backfill, limit=args.limit, page_size=args.page_size)
        )
    elif args.command == "derive":
        asyncio.run(
            _derive(settings, backfill=args.backfill, limit=args.limit, page_size=args.page_size)
        )
    elif args.command == "search":
        asyncio.run(
            _search(
                settings,
                query=args.query,
                mode=args.mode,
                limit=args.limit,
                filters=_filters_from(args),
            )
        )
    elif args.command == "suggest":
        asyncio.run(_suggest(settings, prefix=args.prefix, limit=args.limit))
    elif args.command == "similar":
        asyncio.run(
            _similar(
                settings,
                title_id=None if args.title_id is None else _as_uuid(args.title_id, "title id"),
                limit=args.limit,
                rebuild=args.rebuild,
            )
        )
    elif args.command == "home":
        asyncio.run(_home(settings, limit=args.limit, repeat=args.repeat))
    elif args.command == "push":
        asyncio.run(_push(settings, source_name=args.source, probe=args.probe))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
