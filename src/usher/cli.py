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
from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.adapters.tmdb import TmdbClient, TmdbMetadataProvider
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresRawPayloadStore, PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import TitleKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.bulk import ImdbTitle
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import BulkCatalogRepository
from usher.ports.source import SourceAdapter
from usher.services.bootstrap import BootstrapService
from usher.services.enrich import EnrichService
from usher.services.handlers import (
    SourceBinding,
    enrich_handler,
    match_handler,
    watch_history_handler,
)
from usher.services.ingest import IngestService
from usher.services.jobs import Handler, JobWorker
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.watch_sync import WatchStateSyncService
from usher.telemetry import QueueSnapshot, configure_telemetry, register_queue_gauges

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


@dataclass(frozen=True, slots=True)
class _Pipeline:
    """Every M4 service and the repositories behind them, on one session.

    This is what a composition root *is*: the one place allowed to know that
    `ReconcileService`'s `MediaItemRepository` and `WatchStateSyncService`'s
    are the same table. `services/` may not import `db/` (ADR-0009), so
    nothing below this line could assemble itself.

    Held as ports rather than as the `Postgres*` classes that fill them, so
    the assembly reads as the wiring diagram rather than as a second copy of
    the implementation list.
    """

    sources: PostgresSourceRepository
    credentials: PostgresCredentialStore
    media_items: PostgresMediaItemRepository
    runs: PostgresSyncRunRepository
    queue: PostgresJobQueue
    adapters: ConfiguredSourceAdapterFactory
    matcher: MatchService
    reconcile: ReconcileService
    watch: WatchStateSyncService


def _build_pipeline(
    session: AsyncSession,
    settings: Settings,
    *,
    max_retract_fraction: float | None = None,
    provider: MetadataProvider | None = None,
) -> _Pipeline:
    """Wire one session into the whole ingest pipeline.

    `max_retract_fraction` overrides `settings.sync_max_retract_fraction`;
    `usher sync --allow-full-retraction` is the only caller that passes one,
    and it passes `1.0` (ADR-0015).

    `provider` is `None` for every path that runs *inside a walk*. That is
    not an omission: `MatchService`'s batch path must never make a network
    call per unmatched item, and its constructor takes the provider as
    optional for exactly that reason. Only `work` -- which runs the queued
    `match` and `enrich` handlers -- passes one.
    """
    sources = PostgresSourceRepository(session)
    credentials = PostgresCredentialStore(session, settings.secret_key)
    media_items = PostgresMediaItemRepository(session)
    runs = PostgresSyncRunRepository(session)
    matching = PostgresTitleMatchRepository(session)
    queue = PostgresJobQueue(
        session,
        max_attempts=settings.job_max_attempts,
        backoff_seconds=settings.job_backoff_seconds,
    )
    matcher = MatchService(
        titles=PostgresTitleRepository(session),
        matching=matching,
        queue=queue,
        provider=provider,
    )
    return _Pipeline(
        sources=sources,
        credentials=credentials,
        media_items=media_items,
        runs=runs,
        queue=queue,
        adapters=_adapter_factory(settings),
        matcher=matcher,
        reconcile=ReconcileService(
            ingest=IngestService(
                matcher=matcher,
                matching=matching,
                media_items=media_items,
                episodes=PostgresEpisodeRepository(session),
                queue=queue,
            ),
            media_items=media_items,
            runs=runs,
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
            watch_states=PostgresWatchStateRepository(session),
            runs=runs,
            queue=queue,
            commit=session.commit,
            batch_size=settings.sync_batch_size,
        ),
    )


def _adapter_factory(settings: Settings) -> ConfiguredSourceAdapterFactory:
    return ConfiguredSourceAdapterFactory(
        page_size=settings.source_page_size,
        timeout_seconds=settings.source_timeout_seconds,
        reauth_cooldown_seconds=settings.source_reauth_cooldown_seconds,
    )


async def _open_adapter(pipeline: _Pipeline, source: Source) -> SourceAdapter | None:
    """Build the adapter for one source, or explain why not.

    `None` rather than a raise: an operator running `usher sync` across three
    sources needs the second and third to run when the first's credential row
    has gone missing -- exactly the reasoning `ReconcileService.reconcile`
    applies one layer down to an unreachable server.
    """
    credentials = await pipeline.credentials.get(source.credentials_ref)
    if credentials is None:
        print(f"{source.name}: no stored credentials; re-enter them to reconnect")
        return None
    return pipeline.adapters.build(source, credentials)


async def _selected_sources(pipeline: _Pipeline, name: str | None) -> list[Source]:
    """Every enabled source, or the one named.

    A disabled source is skipped even when named explicitly: `enabled` is how
    an operator parks a server that is being rebuilt, and honouring the name
    over the flag would walk it anyway.
    """
    sources = [source for source in await pipeline.sources.list_all() if source.enabled]
    if name is None:
        return sources
    return [source for source in sources if source.name == name]


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
        pipeline = _build_pipeline(
            session, settings, max_retract_fraction=1.0 if allow_full_retraction else None
        )
        sources = await _selected_sources(pipeline, source_name)
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
        pipeline = _build_pipeline(session, settings)
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
        pipeline = _build_pipeline(session, settings)
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
    """Run queued jobs: `match`, `enrich`, `watch_history`.

    Owns the one `httpx.AsyncClient` behind `TmdbClient`, because the token
    bucket that keeps this deployment under TMDb's ~40 rps ceiling lives on
    the client. A client per job would give every job its own budget, which
    is a rate limiter that limits nothing.
    """
    async with _session_for(settings) as session:
        provider, aclose = await _metadata_provider(settings)
        pipeline = _build_pipeline(session, settings, provider=provider)
        registry = _SourceRegistry(pipeline)
        gauges = _QueueGauges(pipeline.queue)
        register_queue_gauges(gauges.read)
        try:
            worker = JobWorker(pipeline.queue, session.commit, batch_size=settings.job_batch_size)
            worker.register(
                JobKind.MATCH,
                match_handler(pipeline.matcher, pipeline.media_items, registry.resolve),
            )
            worker.register(
                JobKind.WATCH_HISTORY,
                watch_history_handler(
                    pipeline.watch, registry.resolve, user_id=await ensure_default_user(session)
                ),
            )
            if provider is not None:
                worker.register(JobKind.ENRICH, _enrich_handler(session, settings, provider))
            else:
                # Not a silent skip: PRD 08's "TMDb key missing" degradation
                # is a *narrowed* deployment, and an operator whose enrich
                # queue never drains has to be able to see why.
                logger.warning("no TMDb API key configured; enrich jobs will not be claimed")
            # PRD 08: "startup requeues anything left in_progress". Before
            # the first claim, so a previous process's abandoned claims are
            # this one's work rather than nobody's.
            await worker.startup()
            ran = await worker.run_once()
            await gauges.refresh()
            print(f"{ran} jobs")
            while not once:
                if ran == 0:
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                ran = await worker.run_once()
                await gauges.refresh()
        finally:
            await registry.aclose()
            await aclose()


class _QueueGauges:
    """The `jobs` table as PRD 10's two gauges see it.

    A held snapshot, refreshed after every pass, because an OTel observable
    callback runs on the reader's background thread and cannot await an
    asyncpg query -- `register_queue_gauges`' docstring has the whole
    argument. Refreshing after each pass rather than before it means the
    reported depth is the depth *left over*, which is the number "ingest
    stalled" (PRD 10's alert) is actually about.
    """

    __slots__ = ("_queue", "_snapshot")

    def __init__(self, queue: PostgresJobQueue) -> None:
        self._queue = queue
        self._snapshot = QueueSnapshot()

    def read(self) -> QueueSnapshot:
        return self._snapshot

    async def refresh(self) -> None:
        depth = await self._queue.depth()
        parked = await self._queue.parked(limit=1000)
        counts = dict.fromkeys((kind.value for kind in JobKind), 0)
        for job in parked:
            counts[job.kind.value] += 1
        self._snapshot = QueueSnapshot(
            queued={kind.value: count for kind, count in depth.items()}, parked=counts
        )


def _enrich_handler(
    session: AsyncSession, settings: Settings, provider: MetadataProvider
) -> Handler:
    return enrich_handler(
        EnrichService(
            titles=PostgresTitleRepository(session),
            episodes=PostgresEpisodeRepository(session),
            payloads=PostgresRawPayloadStore(session),
            provider=provider,
            commit=session.commit,
            cache_max_age_days=settings.enrich_cache_max_age_days,
        )
    )


async def _metadata_provider(
    settings: Settings,
) -> tuple[MetadataProvider | None, Callable[[], Awaitable[None]]]:
    """The TMDb provider and the callable that closes its transport.

    Returns `(None, no-op)` when no key is configured, rather than raising:
    `match` and `watch_history` jobs need no provider at all, and a worker
    that refused to start without a TMDb key would take two working lanes
    down with the third.
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


class _SourceRegistry:
    """`external_id` -> the configured source that addresses it.

    `Job.key` for `match` and `watch_history` is a source's own
    `external_id` (`usher.domain.jobs.Job` says why: turning it into a
    `MediaItem.id` at enqueue time would cost a round trip per item, 1.1M a
    walk). So a handler has to find *which* source that string belongs to,
    and a household with two servers means a worker bound to one of them
    silently drops the other's jobs.

    Adapters are built lazily and cached for the life of the worker: one
    adapter is one connection pool, and building one per job would
    re-authenticate against the upstream every time.
    """

    def __init__(self, pipeline: _Pipeline) -> None:
        self._pipeline = pipeline
        self._adapters: dict[uuid.UUID, SourceAdapter] = {}

    async def resolve(self, external_id: str) -> SourceBinding | None:
        for source in await self._pipeline.sources.list_all():
            if not source.enabled:
                continue
            stored = await self._pipeline.media_items.get_by_external_id(source.id, external_id)
            if stored is None:
                continue
            adapter = self._adapters.get(source.id)
            if adapter is None:
                adapter = await _open_adapter(self._pipeline, source)
                if adapter is None:
                    return None
                self._adapters[source.id] = adapter
            return SourceBinding(source=source, adapter=adapter)
        return None

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
        self._adapters.clear()


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
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
