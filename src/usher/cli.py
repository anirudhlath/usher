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
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.api.lanes import LaneSupervisor
from usher.composition import (
    NO_CREDENTIALS,
    DefaultUserId,
    Pipeline,
    QueueGauges,
    SourceRegistry,
    build_pipeline,
    build_worker,
    embedder,
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
from usher.db.users import ensure_default_user
from usher.domain.enums import TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.bulk import ImdbTitle
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.ports.repository import BulkCatalogRepository
from usher.ports.source import SourceAdapter
from usher.services.bootstrap import BootstrapService
from usher.telemetry import configure_telemetry, register_queue_gauges

PHASES = ("imdb", "tmdb-ids", "crosswalk", "all")
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
            logger.info("catalog now holds {count} titles", count=await catalog.count_titles())
    finally:
        await client.aclose()
        await engine.dispose()


async def _status(settings: Settings) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            runs = await PostgresImportRunRepository(session).list_runs()
            catalog_size = await PostgresBulkCatalogRepository(session).count_titles()
    finally:
        await engine.dispose()
    # Printed, not logged: this is a report an operator asked for, and routing
    # it through the JSON log sink would make it unreadable at a terminal.
    print(f"titles in catalog: {catalog_size}")
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
    """Run queued jobs: `match`, `enrich`, `watch_history`, `index`.

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
        pipeline = build_pipeline(session, settings, provider=provider)
        registry = SourceRegistry(pipeline)
        gauges = QueueGauges()
        register_queue_gauges(gauges.read)
        try:
            worker = build_worker(
                pipeline,
                settings,
                provider=provider,
                embedder=model,
                resolve=registry.resolve,
                user_id=await ensure_default_user(session),
            )
            # PRD 08: "startup requeues anything left in_progress". Before
            # the first claim, so a previous process's abandoned claims are
            # this one's work rather than nobody's.
            await worker.startup()
            ran = await worker.run_once()
            await gauges.refresh(pipeline.queue)
            print(f"{ran} jobs")
            while not once:
                if ran == 0:
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                ran = await worker.run_once()
                await gauges.refresh(pipeline.queue)
        finally:
            await registry.aclose()
            await aclose()
            await aclose_model()


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
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        model = settings.embedding_model
        stale = await pipeline.embeddings.count_stale(model)
        if not backfill:
            refused = await pipeline.embeddings.count_refused(model)
            print(f"model: {model}")
            print(f"stale embeddings: {stale}")
            print(f"refused (no content to embed): {refused}")
            # ~115 tokens a document at ~8,000-10,700 tokens/s on CPU. A range
            # derived from the invariant rather than from a texts/s rate.
            print(f"estimated worker time: {stale * 115 / 10700:.0f}-{stale * 115 / 8000:.0f}s")
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
        print(f"{seen} stale titles swept, {written} index jobs written")


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
        if not rows and await pipeline.similar.computed_at() is None:
            # Two causes for an empty answer and only one is a fact about the
            # title. One message for both sends an operator to look at the
            # wrong thing.
            print("no neighbours have ever been computed -- run `usher similar --rebuild`")
        elif not rows:
            print("no neighbours for this title")


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
    if args.command == "similar" and bool(args.title_id) == bool(args.rebuild):
        # Both spellings refused: no arguments is a read of nothing, and both
        # together is a read and a write in one command. `parser.error` again
        # -- exit 2 with usage rather than exit 1 with a traceback.
        parser.error("give a title id, or --rebuild, but not both")
    return args


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
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(list(argv) if argv else ["serve"])
    settings = get_settings()
    configure_telemetry(settings)
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
    elif args.command == "similar":
        asyncio.run(
            _similar(
                settings,
                title_id=None if args.title_id is None else _as_uuid(args.title_id, "title id"),
                limit=args.limit,
                rebuild=args.rebuild,
            )
        )
    elif args.command == "push":
        asyncio.run(_push(settings, source_name=args.source, probe=args.probe))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
