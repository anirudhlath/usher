"""Command-line composition root: `python -m usher <command>`."""

import argparse
import asyncio
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher import __version__
from usher.api.lanes import LaneSupervisor
from usher.composition import (
    NO_CREDENTIALS,
    DefaultUserId,
    Pipeline,
    QueueGauges,
    SearchGauges,
    SourceRegistry,
    build_curation_service,
    build_derive_service,
    build_pipeline,
    build_scheduler,
    build_worker,
    embedder,
    llm_client,
    metadata_provider,
    nothing,
    open_adapter,
    run_bootstrap,
    selected_sources,
    unit_of_work,
)
from usher.config import Settings, get_settings, settings_rejection
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.backup import PostgresBackupRepository, PostgresRestoreRepository
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.credentials import PostgresCredentialRotationStore, build_cipher
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.users import default_user, ensure_default_user
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.eval.errors import EvalDependencyMissing
from usher.eval.goldens.suggest import GATE_SEED
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.ports.rows import RowContext
from usher.ports.search import SearchFilters, SearchMode, SuggestTier
from usher.ports.source import SourceAdapter
from usher.services.backup import (
    CREDENTIAL_KEY_WARNING,
    BackupReport,
    BackupService,
)
from usher.services.bootstrap import (
    VocabularyState,
    VocabularyVerdict,
    bootstrap_report,
)
from usher.services.curation import CurationReport
from usher.services.curation_validate import DropReason
from usher.services.genres import GenreNormalisationService
from usher.services.home import ComposeReport, HomeService
from usher.services.jobs import JobWorker, WorkerLoop
from usher.services.reconcile import RETRACTION_ERROR_CODE
from usher.services.restore import RestoreRefused, RestoreReport, RestoreService
from usher.services.rotation import RotationReport, RotationService
from usher.services.rows import ROW_PROVIDERS, enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RowCache
from usher.services.search import SearchAnswer, SemanticSearchUnavailable
from usher.telemetry import (
    configure_telemetry,
    register_queue_gauges,
    register_scheduler_gauges,
    register_search_gauges,
)

# `--phase all` runs `FULL_SEQUENCE` in order -- the six members of
# `BootstrapPhase` that are *steps*. Three of its edges are required ordering
# rather than style.
PHASES = tuple(phase.value for phase in BootstrapPhase)
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
# The failures that are the *operator's* to fix, and so the ones `main` answers with a
# message instead of a stack.
OPERATOR_ERRORS: tuple[type[Exception], ...] = (
    # A refused connection, a name that does not resolve, a full disk, a
    # bulk dataset that is not where it was left.
    OSError,
    # Everything the driver does wrap: a missing table (`alembic upgrade head` never
    # ran), a dead pool, a permission the role does not have.
    DBAPIError,
    # TMDb, Emby, and every bulk download that is *not* behind a port -- and
    # every one of them is behind a port today, which is why the three below
    # exist. Kept because an adapter is free to let one through and because
    # nothing else covers a bare `httpx` call added later.
    httpx.HTTPError,
    # The port taxonomy's transport half. The upstream could not be reached,
    # or did not answer in time -- start the endpoint, fix the URL, wait for
    # the model to load. Also the embedding runtime, whose own adapter says a
    # restart fixes every case it raises this for.
    PortUnavailable,
    # The credential was rejected.
    PortAuthFailed,
    # The upstream asked to be backed off. A CLI has no backoff schedule to
    # apply, so the honest answer at a terminal is the sentence and exit 1.
    PortRateLimited,
)
# 128 + SIGINT, the shell's convention, so a wrapping script can tell an
# operator's Ctrl-C from a command that failed.
_INTERRUPTED_EXIT_CODE = 130


async def _bootstrap(settings: Settings, phase: BootstrapPhase) -> None:
    """One command's session and engine, wrapped around the dispatch both roots share."""
    engine = build_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            await run_bootstrap(
                PostgresBulkCatalogRepository(session),
                PostgresImportRunRepository(session),
                session.commit,
                settings,
                phase,
                report=print,
                events=NullEventPublisher(),
            )
    finally:
        await engine.dispose()


def _vocabulary_line(verdict: VocabularyVerdict) -> str:
    """One line for the decision `services.bootstrap.vocabulary_verdict` made.

    **The sentence stays here and the decision does not**, which is the whole
    shape of `BootstrapReport`: `GET /admin/bootstrap/status` serialises the
    same `VocabularyVerdict` this renders, so the two surfaces cannot disagree
    about what *"not loaded"* means, and the route never ends up serialising
    English. `tests/integration/test_admin_bootstrap.py::test_the_route_and_
    the_cli_report_the_same_vocabulary_verdict` feeds the route's own document
    back through this function and requires the byte-identical line.

    Pure and synchronous — every read it used to make is `vocabulary_verdict`'s
    now. It sits beside the `MIXED RELEASES` line
    `composition._report_coverage` prints for the sibling condition on
    `genome_scores`, and the reason the mixed case
    reads *"not checked"* rather than a verdict is recorded on
    `VocabularyState` itself.
    """
    if verdict.state is VocabularyState.NO_VECTORS:
        return "genome vocabulary: no vectors to name"
    if verdict.state is VocabularyState.MIXED_RELEASES:
        return "genome vocabulary: not checked -- genome_scores holds more than one release"
    if verdict.state is VocabularyState.MISMATCHED:
        # The port's own message, which names *both* release tokens: "the
        # vocabulary is wrong" without saying what is stored is not something
        # an operator can act on.
        return f"genome vocabulary: {verdict.detail}"
    if verdict.state is VocabularyState.NOT_LOADED:
        return "genome vocabulary: not loaded -- run bootstrap --phase movielens"
    return f"genome vocabulary: {verdict.tags} tags"


async def _status(settings: Settings) -> None:
    """`usher bootstrap-status`, printed from the report the route serialises.

    One `BootstrapReport` and four prints, rather than four reads and a
    rendering: the report is what makes this command and
    `GET /admin/bootstrap/status` two views of one answer instead of two
    answers. The reads it makes cost about a third of a second on a real
    1.27M-title catalog — `BootstrapReport`'s own docstring carries the
    numbers and the reason they are not cached.
    """
    async with _session_for(settings) as session:
        report = await bootstrap_report(
            PostgresImportRunRepository(session),
            PostgresBulkCatalogRepository(session),
            PostgresGenomeRepository(session),
        )
    # Printed, not logged: this is a report an operator asked for, and routing
    # it through the JSON log sink would make it unreadable at a terminal.
    print(f"titles in catalog: {report.titles}")
    print(f"genome vectors: {report.genome.with_vector}")
    print(_vocabulary_line(report.vocabulary))
    if not report.runs:
        print("no import has been run yet")
        return
    for run in report.runs:
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
    engine = build_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _sync(
    settings: Settings, *, source_name: str | None, kind: str, allow_full_retraction: bool
) -> None:
    """Walk each selected source: items first, then watch state."""
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
        failed: list[SyncRun] = []
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
                failed.extend(one for one in (run, watch) if one.status is SyncRunStatus.FAILED)
            finally:
                await adapter.aclose()
        if failed:
            raise SystemExit(_sync_failed(failed))


def _sync_failed(runs: Sequence[SyncRun]) -> str:
    """The exit line for a sync in which at least one run recorded `FAILED`.

    The per-run detail is already on stdout above -- including each `error`,
    which for a refusal is the two numbers and the ceiling. This says *which*
    lanes failed and stops the command claiming success, rather than repeating
    what was printed a line earlier.

    **`--allow-full-retraction` is named only when a refusal is among them**,
    and that is the whole reason `RETRACTION_ERROR_CODE` exists. It is the one
    failure here an operator has a command for; a read timeout is not, and an
    escape hatch offered for every failure is one people learn to paste
    without reading. `error_code` is what is matched rather than the refusal's
    English, because that sentence is built from three numbers in
    `ports/ingest.py` and is a standing candidate for rewording.
    """
    lanes = ", ".join(f"{one.kind.value}" for one in runs)
    line = f"{len(runs)} sync run(s) failed: {lanes}; see the lines above and `usher sync-status`"
    if any(one.error_code == RETRACTION_ERROR_CODE for one in runs):
        line += (
            "\nthe availability sweep refused: if the removal was intended, "
            "re-run with `usher sync --allow-full-retraction`"
        )
    return line


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
    """The review queue (PRD 02: "unmatched items are never dropped")."""
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if resolve is not None and title is not None:
            # Both conversions before the read, in the order the arguments
            # were written, so a run with two malformed ids still names the
            # first one rather than the one the new check happens to want.
            # Pinned by `test_two_malformed_ids_name_the_media_item_first`,
            # because swapping these two lines is otherwise a silent mutant.
            media_item_id = _as_uuid(resolve, "media item id")
            title_id = _as_uuid(title, "title id")
            # One extra round trip per hand resolution -- `PostgresTitle Repository.get`
            # is a `session.get` on the primary key, so it is one indexed `SELECT` on a
            # command an operator runs by hand, one line at a time.
            if await pipeline.titles.get(title_id) is None:
                print(f"no such title: {title_id}")
                return
            attached = await pipeline.media_items.attach_title(
                media_item_id,
                title_id=title_id,
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
    """Run queued jobs: `match`, `enrich`, `watch_history`, `index`, `derive`, `curate`."""
    engine = build_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    sessions = build_session_factory(engine)
    provider, aclose = await metadata_provider(settings)
    # Both built once, here, and closed in the same `finally`. A model is
    # a process-lifetime resource for the same reason the TMDb client is:
    # a load is 4.84 s cold / 0.13 s warm over 65 MB of ONNX, and the worker
    # below opens a scope per *job*.
    model, aclose_model = await embedder(settings)
    # And the completion client, on the same terms: one per process, not
    # one per job. `USHER_LLM_ENABLED=false` is the shipped default and
    # answers `(None, no-op)`, which is what leaves `curate` unclaimed
    # here rather than parked.
    client, aclose_client = await llm_client(settings)
    registry = SourceRegistry()
    gauges = QueueGauges()
    register_queue_gauges(gauges.read)
    # PRD 10's embedding backlog, refreshed on the same beat and for the
    # same reason: an OTel observable callback runs on the metric reader's
    # background thread and cannot await an asyncpg query. Refreshed even
    # when this process has no model -- a worker without one leaves index
    # jobs for one that has, and the backlog is the number that says so.
    backlog = SearchGauges()
    register_search_gauges(backlog.read)
    # **A session factory, not a session.** This command held exactly one
    # `AsyncSession` for the life of the process until M9's W1, which is what
    # bound the whole lane to one job at a time: `AsyncSession` is not
    # concurrency-safe, so the worker now opens one per claim and one per job
    # through the same `unit_of_work` the server's lanes use.
    work = unit_of_work(sessions, settings, events=NullEventPublisher(), provider=provider)
    try:
        async with sessions() as bootstrap_session:
            user_id = await ensure_default_user(bootstrap_session)
            await bootstrap_session.commit()
        worker = build_worker(
            work,
            settings,
            provider=provider,
            embedder=model,
            client=client,
            registry=registry,
            user_id=user_id,
        )

        # What this process has taken back from workers that stopped
        # heartbeating. `recover()` returns it, and a caller that discards it
        # leaves a lost worker's claims traceable only through a WARNING; this
        # command has no readiness route, so the total goes in the pass line it
        # already prints rather than growing a surface.
        recovered = 0
        # `None` is *nothing printed yet*, which is the state that makes the
        # startup line unconditional. An `int | None` on `LaneReport` means
        # something else entirely -- *this process runs no worker* -- and
        # `usher work` has no such state.
        printed: int | None = None

        def _took(claims: int) -> None:
            nonlocal recovered
            recovered += claims

        def _report(ran: int) -> None:
            """Print the pass line at startup, then only when the total moves.

            A line per pass is ~17,280 a day at the idle floor, which is the
            rate that trains an operator to ignore output; a line only at
            startup reports a total that is almost always zero, hiding every
            later recovery.
            """
            nonlocal printed
            if printed != recovered:
                print(f"{ran} jobs, {recovered} recovered claims")
                printed = recovered

        async def _refresh() -> None:
            async with work() as pipeline:
                await gauges.refresh(pipeline.queue)
                await backlog.refresh(
                    pipeline.embeddings, pipeline.neighbors, settings.embedding_model
                )

        async def _built() -> JobWorker:
            return worker

        loop = WorkerLoop(
            _built,
            lease_seconds=settings.job_lease_seconds,
            idle_seconds=_IDLE_SLEEP_SECONDS,
            refresh=_refresh,
            recovered=_took,
            failure="the worker pass failed; the daemon continues: {error}",
        )
        if once:
            # ⚠️ **`--once` is outside the daemon's guard**, which is why it
            # calls the unguarded pass. A cron entry reads the *exit code*, and
            # a guard here would answer a crashed pass with `0` -- so the thing
            # that exists to notice would be the last to. The daemon has no
            # exit code and its survival is the property instead.
            _report(await loop.pass_once())
        else:
            await loop.run(after=_report)
    finally:
        await registry.aclose()
        await aclose()
        await aclose_model()
        await aclose_client()
        await engine.dispose()


async def _schedule(settings: Settings, *, once: bool) -> None:
    """Run the scheduled-work loop, or one tick of it."""
    engine = build_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    try:
        scheduler = build_scheduler(settings, sessions=build_session_factory(engine))
        register_scheduler_gauges(scheduler.read)
        registered = len(scheduler.jobs)
        if once:
            ran = await scheduler.tick()
            # Both numbers, because `ran` alone cannot distinguish "nothing
            # was due" from "nothing is registered", and the second reads as a
            # healthy night on a deployment whose scheduler can do nothing.
            print(f"{ran} of {registered} scheduled jobs ran")
            return
        print(f"scheduling {registered} jobs every {settings.scheduler_tick_seconds:g}s")
        await scheduler.run()
    finally:
        await engine.dispose()


async def _derive(settings: Settings, *, backfill: bool, limit: int, page_size: int) -> None:
    """Report derivation coverage, or re-derive people, credits, collections and artwork inline."""
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
        # **Expect this to be small against a large cache, and that is not a
        # defect**: `images` joined `*_APPEND_TO_RESPONSE` in M4, so a payload
        # cached before then derives only its two top-level primaries.
        print(f"images written: {report.images_written:,}")


async def _index(settings: Settings, *, backfill: bool, limit: int, page_size: int) -> None:
    """Report the search index's freshness, or enqueue the work that fixes it."""
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
            # ~135 tokens a document at ~8,000-10,700 tokens/s on CPU.
            print(
                f"estimated worker time: {snapshot.stale * 135 / 10700:.0f}-"
                f"{snapshot.stale * 135 / 8000:.0f}s"
            )
            return

        written = seen = 0
        after: uuid.UUID | None = None
        while True:
            # Task 9's cursor, imported rather than re-derived.
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
            # **The cursor advances on the last id of the page, always** -- never on
            # "how many were still stale afterwards".
            after = page[-1].id
            if limit and seen >= limit:
                break
        # After the sweep, not inside it. The predicate is cheap to count and
        # cheaper still not to count per page, and the number an operator wants
        # is the backlog *left over* -- the same reason `QueueGauges` refreshes
        # after a worker pass rather than before it.
        await gauges.refresh(pipeline.embeddings, pipeline.neighbors, model)
        print(f"{seen} stale titles swept, {written} index jobs written")


async def _genres(
    settings: Settings,
    *,
    backfill: bool,
    batch_size: int,
    limit: int,
    after: uuid.UUID | None,
) -> None:
    """Report `titles.genres`' spelling, or rewrite it into Usher's vocabulary."""
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        service = GenreNormalisationService(
            titles=pipeline.titles,
            embeddings=pipeline.embeddings,
            commit=session.commit,
            model_name=settings.embedding_model,
        )
        report = await service.normalise(
            batch_size=batch_size, limit=limit, after=after, write=backfill
        )
    print(f"rows scanned: {report.rows_scanned:,}")
    if not backfill:
        # Named for what they are on a run that wrote nothing. "rows
        # rewritten: 4" printed by a command that did not rewrite anything is
        # the report reading as the thing it declined to do.
        print(f"rows to rewrite: {report.rows_rewritten:,}")
        print(f"rows already canonical: {report.rows_unchanged:,}")
        return
    print(f"rows rewritten: {report.rows_rewritten:,}")
    print(f"rows unchanged: {report.rows_unchanged:,}")
    # **Expect this to be far smaller than the rewrite count, and that is the finding
    # rather than a defect**: the embedded population is the enriched tier and the
    # source spellings are almost entirely on skeletons.
    print(f"embeddings staled: {report.embeddings_staled:,}")
    if report.last_id is not None:
        print(f"resume after: {report.last_id}")


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
    """PRD 05's search, at a terminal."""
    requested = SearchMode(mode)
    model, aclose_model = (
        # `report=False`: that factory's warning is about a *lane* ("index jobs will not
        # be claimed"), which is right for `usher work` and wrong twice over here -- it
        # advises about work this process does not do, and `cli.py`'s printed-not-logged
        # rule makes it a JSON envelope in front of the results.
        await embedder(settings, report=False)
        if requested is not SearchMode.FULL_TEXT
        else (None, nothing)
    )
    # After the embedder, and reading its answer: `model is None` is the narrowed
    # deployment, and there is nothing to expand for.
    client, aclose_client = (
        await llm_client(settings, report=False)
        if model is not None and settings.query_expansion_enabled
        else (None, nothing)
    )
    try:
        async with _session_for(settings) as session:
            pipeline = build_pipeline(session, settings, embedder=model, llm=client)
            try:
                answer = await pipeline.search.search(
                    query,
                    mode=requested,
                    limit=limit,
                    filters=filters,
                    # `ensure_default_user`, not `default_user`: this command needs an
                    # id and nothing else, exactly as `usher curate` does, and PRD 01's
                    # authentication seam is a singleton row until a request has one to
                    # carry.
                    user_id=await ensure_default_user(session),
                )
            except SemanticSearchUnavailable as exc:
                # Not narrowed to full-text, and the service is right to refuse
                # rather than answer: the caller asked the one question
                # full-text cannot answer and would otherwise get a plausible
                # answer to a different one. `SystemExit` with a sentence, the
                # treatment `_as_uuid` gives a bad id.
                raise SystemExit(f"{exc} -- try --mode fused, or run `usher index`") from exc
    finally:
        await aclose_client()
        await aclose_model()

    _print_search_answer(answer)


def _print_search_answer(answer: SearchAnswer) -> None:
    """The operator's answer.

    `print`, never `logger` -- `_print_home_report`'s and `_print_curation_report`'s
    split, and the same reason: a command's answer is stdout.

    A function of its own rather than a tail of `_search`, for
    `_print_curation_report`'s reason: everything above it needs a database and
    everything here needs a `SearchAnswer`, and the expanded-query line is the
    one report in this milestone whose *absence* is the defect.
    """
    if answer.expanded_query is not None:
        # **Before the results, because it is the question they answer.** Reported on
        # every search that bought a rewrite, not only when it looks surprising: a
        # viewer who searched for one thing and got results for another cannot tell a
        # good expansion from a bad one without seeing it, and neither can an operator
        # reading their bug report.
        print(f"expanded: {answer.expanded_query}")
    for rank, result in enumerate(answer.results, start=1):
        year = f" ({result.year})" if result.year else ""
        owned = "*" if result.owned else " "
        print(f"{rank:>3} {owned} {result.score:6.4f}  {result.name}{year}  {result.title_id}")
    if not answer.results:
        print("no match")
    # Always, not only when it is low: a number an operator sees only when something is
    # wrong is a number they have no baseline for.
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
        # between a diagnostic and a complaint. "Enriched", not "filtered":
        # that is the population `_COVERAGE` counts, and the sentence has to
        # name the same rows the number did or an operator whose catalog is
        # mostly skeletons reads it as a claim about the catalog.
        print(
            "warning: no enriched title in the filtered population has an embedding, so this "
            "was full-text only -- run `usher index --backfill`"
        )


async def _suggest(settings: Settings, *, prefix: str, limit: int, tier: str) -> None:
    """Type-ahead, at a terminal, from whichever of the two tiers is asked for."""
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        results = await pipeline.search.suggest(
            prefix,
            limit=limit,
            tier=SuggestTier(tier),
            user_id=await ensure_default_user(session),
        )
    for result in results:
        year = f" ({result.year})" if result.year else ""
        print(f"{result.score:6.4f}  {result.name}{year}  {result.title_id}")
    if not results:
        print("no match")


async def _eval(
    settings: Settings, *, surface: str | None, full: bool, seed: int, sample: int
) -> None:
    """Measure a surface's quality against bars written down before the run.

    **Not a test.** It reads a real catalog and drives the real services; it
    creates nothing and writes only to the `eval` schema, and only with
    `--full`.

    The `eval` extra is checked here rather than at import: `usher --help`
    must work on a deployment that has never installed it, and a bare
    `ModuleNotFoundError: ranx` tells an operator a module is absent and
    nothing else.
    """
    try:
        from usher.eval.suggest_run import run_suggest
        from usher.eval.verdicts import exit_code_for
    except EvalDependencyMissing as problem:
        raise SystemExit(str(problem)) from problem

    if surface not in (None, "suggest"):
        raise SystemExit(f"no eval surface named {surface!r}")

    async with _session_for(settings) as session:
        report = await run_suggest(session, settings, full=full, seed=seed, sample=sample)
    for line in report.lines:
        print(line)
    raise SystemExit(exit_code_for(report.verdict))


async def _similar_status(pipeline: Pipeline) -> None:
    """How old `title_neighbors` is, and how much of it disagrees with the blend."""
    computed_at = await pipeline.similar.computed_at()
    if computed_at is None:
        print("no neighbours have ever been computed -- run `usher similar --rebuild`")
    else:
        age = datetime.now(UTC) - computed_at
        # Hours: a day-resolution line cannot distinguish "finished an hour
        # ago" from "finished this morning", which is the comparison an
        # operator makes.
        print(
            f"neighbour table's oldest row: {computed_at.isoformat()} "
            f"({age.total_seconds() / 3600:.1f}h old)"
        )
    stale = await pipeline.similar.stale_neighbors()
    if stale:
        print(f"{stale} neighbour rows were computed under a different blend -- rebuild to clear")
    elif computed_at is not None:
        print("no neighbour row disagrees with the running blend")


async def _similar(
    settings: Settings,
    *,
    title_id: uuid.UUID | None,
    limit: int,
    rebuild: bool,
    resume: bool = False,
    max_seeds: int | None = None,
) -> None:
    """Report the table's age, read one title's neighbours, or recompute it."""
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if rebuild:
            report = await pipeline.similar.rebuild(resume=resume, max_seeds=max_seeds)
            print(f"rebuilt {report.seeds} seeds, wrote {report.rows} neighbour rows")
            # **The genome's coverage, with its denominators, printed by the path that
            # reads the vectors.** PRD 05 promised "~7%" since before an importer
            # existed and never said of what; these are the two numbers that answer it,
            # and the second is the one that decided whether the term could promote
            # anything.
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
                    f"carried a genome vector on both sides ({pair_share:.2f}%) "
                    "-- measured, not blended"
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

        if title_id is None:
            # The whole-table form. After the rebuild branch above, `None`
            # here can only mean "no arguments at all", which `parse_args`
            # now allows and used to refuse.
            await _similar_status(pipeline)
            return
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
    """Compose the home screen, and time it."""
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        user = await default_user(session)
        # The same wiring `api/deps.py` builds per request, minus the request: `taste`
        # and `affinities` are values the composer hands over, because a provider may
        # import only `domain/` and `ports/`.
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
            affinities=lambda: pipeline.taste.genre_affinity(user.id),
            curated=pipeline.curated_rows,
            images=pipeline.images,
        )
        cache = RowCache(clock=lambda: datetime.now(UTC))
        # **The same table `GET /home` filters against, read by the same join.** A
        # setting honoured by one composition root and not the other is two different
        # products, and this is the root an operator reaches for when a shelf is missing
        # -- so it must not be the one that still shows it.
        provider_settings = row_provider_settings(
            await pipeline.row_provider_settings.overrides(), pipeline.row_providers
        )
        disabled = [one.slug for one in provider_settings if not one.enabled]
        # **No refresher, and the `None` is the decision rather than an omission.**
        # `HomeService` gates its stale-serve grace window on having one, so this
        # composer is M7's fresh-or-miss cache exactly as before -- which is what keeps
        # the cold/warm pair below meaning what it has always meant.
        service = HomeService(
            enabled_row_providers(provider_settings), cache=cache, refresh=None, max_rows=limit
        )

        # Collected rather than overwritten so the last one is reachable
        # without an `Optional` no input can reach -- `parse_args` refuses
        # `--repeat 0`, and `assert` is not available in shipped code.
        reports: list[ComposeReport] = []
        for _ in range(repeat):
            # Cleared *before* each run, so every one is cold: otherwise the
            # second run is a cache hit and the number times a dict lookup.
            cache.clear()
            reports.append(await service.compose_report(ctx))
        report = reports[-1]
        cold = [one.duration_seconds for one in reports]

        warm_at = time.perf_counter()
        await service.compose(ctx)
        warm = time.perf_counter() - warm_at

        _print_home_report(report, cold=cold, warm=warm, disabled=disabled)


def _print_home_report(
    report: ComposeReport, *, cold: Sequence[float], warm: float, disabled: Sequence[str]
) -> None:
    """The operator's table.

    `print`, never `logger` -- the split every command in this module makes: loguru
    output is operational and goes to a sink an operator may not be reading, and a
    command's answer is stdout, which is what gets piped.

    **`disabled` is printed unconditionally**, on exactly the argument the
    revisit rule below is printed unconditionally for. A disabled provider has
    **no line in the table at all** -- it never proposed, so `ComposeReport`
    has no entry to print -- which is indistinguishable from a provider that
    was deleted, and it is the state an operator is looking at when they run
    this command to find out where a shelf went. Printing "none" is what lets
    them rule the cause out; printing nothing when nothing is disabled would
    make the absence of the line the answer, which is a thing nobody reads.
    """
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
    # The registry's own size rather than a literal ten, and the arithmetic is
    # stated so the two numbers above and below cannot disagree silently: the
    # table has one line per *composed* provider, and the difference between
    # that and the registry is exactly this list.
    print(
        f"disabled by an operator: {', '.join(disabled) if disabled else 'none'} "
        f"({len(disabled)} of {len(ROW_PROVIDERS)} registered; "
        f"PUT /admin/rows/providers/{{slug}} to change)"
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
    # the other nine converges on that provider's latency and buys nothing --
    # the finding is a query to fix.
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


async def _curate(settings: Settings) -> None:
    """One generation for the default household, at a terminal."""
    # Built before the session and released in the same `finally` as
    # `usher search`'s embedder: it is a once-per-process resource
    # (`composition.llm_client` opens an `httpx.AsyncClient` with its own
    # pool), which for a command is once.
    client, aclose_client = await llm_client(settings, report=False)
    if client is None:
        # Before the `try`, because there is nothing to release: the factory
        # hands back `composition.nothing` on this path, and awaiting a
        # shared module-level no-op would read as cleanup that happened.
        raise SystemExit(
            "usher curate: this deployment has no LLM, so there is no generation to run "
            "(set USHER_LLM_ENABLED=true and point USHER_LLM_BASE_URL at an "
            "OpenAI-compatible endpoint)"
        )
    try:
        async with _session_for(settings) as session:
            pipeline = build_pipeline(session, settings)
            service = build_curation_service(pipeline, settings, client)
            # `ensure_default_user`, not `default_user`: this command needs an
            # id and nothing else, and PRD 01's authentication seam is a
            # singleton row until M9 gives it a request to come from.
            user_id = await ensure_default_user(session)
            try:
                report = await service.generate(user_id)
            except PortDataMalformed as exc:
                raise SystemExit(
                    f"usher curate: {exc}\n(the household's previous rows still stand)"
                ) from exc
            _print_curation_report(report)
    finally:
        await aclose_client()


def _print_curation_report(report: CurationReport) -> None:
    """The operator's answer.

    `print`, never `logger` -- `_print_home_report`'s split, and the same reason: a
    command's answer is stdout.

    Every number here comes off the `CurationReport` rather than being
    re-derived. **The pool size is the one that could not be re-derived
    honestly**: asking `CandidatePoolService` again would build a *second*
    pool -- a second catalog read, a second centroid, and a number equal to
    the first only by luck of nothing having been watched in between -- and
    summing the rows that came back cannot see the rows that are missing.

    `cost_usd` is a `Decimal` and stays one all the way to the screen. Eight
    decimal places because that is what `llm_calls.cost_usd`'s
    `NUMERIC(12, 8)` stores, so this line and
    `SELECT sum(cost_usd) FROM llm_calls` show an operator the same digits.
    """
    print(f"generation: {report.generation_id}")
    print(f"pool: {report.pool_size} candidates")
    kept = len(report.rows)
    cards = sum(len(row.card_title_ids) for row in report.rows)
    print(f"kept: {kept} {_unit('row', kept)}, {cards} {_unit('card', cards)}")
    for row in report.rows:
        count = len(row.card_title_ids)
        print(f"  {row.slug:<14}{row.title:<48}{count:>3} {_unit('card', count)}")
    # **All five, zeros included**, iterating the map the validator built
    # rather than filtering it: a reason absent from a report is
    # indistinguishable from a reason nobody counts, which is the tally's own
    # subject one level up -- and at a terminal there is no second export to
    # compare against.
    print("dropped (all five reasons, zeros included -- an absent line and a")
    print("         reason nobody counts read the same):")
    for reason, count in report.dropped.items():
        print(f"  {reason.value:<16}{count:>4} {_unit(_drop_unit(reason), count)}")
    usage = report.usage
    print(
        f"tokens: {usage.tokens_in} in, {usage.tokens_out} out   "
        # Never `float(...)`: the two disagree below the column's own
        # precision, which is where a per-token price on a cheap model lands.
        f"cost: ${usage.cost_usd:.8f}   "
        f"latency: {usage.latency_ms} ms   "
        # What answered, not what was asked -- PRD 10 groups spend by model
        # and a proxy serving a different one is the state
        # `curated_rows.model_name` exists to make queryable.
        f"model: {usage.model}"
    )


def _drop_unit(reason: DropReason) -> str:
    """`row` or `card`, read off the member's own name.

    Two of the five count rows and three count cards, so summing across the
    label is meaningless -- and the `row_` prefix is what
    `curation_validate`'s vocabulary uses to say so out loud. Derived rather
    than tabulated here, because a table is a second copy that a sixth member
    can arrive without a row in.

    Singular, because the noun and the number that agrees with it are two
    decisions and only one of them is about the vocabulary. `_unit` makes the
    other.
    """
    return "row" if reason.value.startswith("row_") else "card"


def _unit(noun: str, count: int) -> str:
    """`1 row`, `2 rows` -- the form of the noun `count` agrees with.

    Cosmetic and worth the function anyway: three lines of this report format
    a count beside a unit (`kept:`, each kept row's cards, each drop reason),
    so a plural hardcoded at one of them leaves the other two printing
    `1 cards` -- and a report whose own prose reads unproofed invites the
    numbers beside it to be read the same way.
    """
    return noun if count == 1 else f"{noun}s"


async def _backup(settings: Settings, *, output: Path | None) -> None:
    """Write one artifact holding everything nothing else can rebuild."""
    async with _session_for(settings) as session:
        service = BackupService(repository=PostgresBackupRepository(session))
        report = await service.write(output)
    _print_backup_report(report)


#: How many refused rows `_print_restore_report` names before it summarises.
_REFUSALS_NAMED: Final = 20


async def _restore(
    settings: Settings, *, artifact: Path, dry_run: bool, skip_unresolvable: bool
) -> None:
    """Merge one artifact into this database, in one transaction, or refuse it."""
    async with _session_for(settings) as session:
        service = RestoreService(
            repository=PostgresRestoreRepository(session),
            commit=session.commit,
            rollback=session.rollback,
        )
        try:
            report = await service.restore(
                artifact, dry_run=dry_run, skip_unresolvable=skip_unresolvable
            )
        except RestoreRefused as exc:
            raise SystemExit(f"usher restore: {exc}") from exc
    _print_restore_report(report)
    if report.refused:
        raise SystemExit(_restore_refused(report))


def _restore_refused(report: RestoreReport) -> str:
    """The exit line for a run that refused, and what an operator can do next.

    Two kinds of refusal, and only one is an errand: a reference naming a
    provider id is something an importer fixes, and one naming only a raw id is
    not. The ladder's third rung is a check on the target rather than a key, so
    a title reaches it only by carrying neither provider id -- an unmatched stub
    that is in no IMDb dump, has no TMDb id to enrich by, and gets a new UUID on
    every rebuild. The second case names the flag rather than an errand.
    """
    refused = len(report.refused)
    unfindable = sum(1 for one in report.refused if all(key.startswith("id=") for key in one.keys))
    line = (
        f"{refused:,} {_unit('row', refused)} could not be restored, so nothing was. "
        f"Per table: {dict(report.refused_by_table())}"
    )
    if unfindable:
        line += (
            f"\n{unfindable:,} of them name only a raw id, which **no importer can "
            "supply**: the title carried neither an imdb_id nor a tmdb_id when the "
            "backup was written, so it is in no dump and a rebuild mints it a new one. "
            "Re-run with `--skip-unresolvable` to drop those rows and restore the rest "
            "-- the next `usher sync` re-derives the links among them."
        )
    if unfindable < refused:
        line += (
            "\nThe rest name a provider id: import or enrich what the lines above "
            "name, then run it again."
        )
    return line


def _print_restore_report(report: RestoreReport) -> None:
    """Five counts per table, then the refusals by name up to a cap, then one summary."""
    for table, outcome in sorted(report.outcomes.items()):
        counts = (
            f"{outcome.written:>9,} written"
            f"{outcome.present:>10,} present"
            f"{outcome.absent:>10,} nothing to write onto"
        )
        if outcome.unresolved:
            counts += f"{outcome.unresolved:>10,} skipped as unresolvable"
        if outcome.refused:
            counts += f"{len(outcome.refused):>10,} refused"
        print(f"  {table:<24}{counts}")
    for refusal in report.refused[:_REFUSALS_NAMED]:
        print(f"  refused {refusal.table:<16}{', '.join(refusal.keys)} -- {refusal.reason}")
    if len(report.refused) > _REFUSALS_NAMED:
        rest = len(report.refused) - _REFUSALS_NAMED
        print(
            f"  … and {rest:,} more refused, not named here. The per-table counts "
            "above are exact; `--dry-run` reports the same list without holding a "
            "transaction open."
        )
    if report.total_unresolved:
        print(
            f"{report.total_unresolved:,} "
            f"{_unit('row', report.total_unresolved)} skipped as unresolvable "
            "(--skip-unresolvable): every one names a title or episode this catalog "
            "does not hold, and the next `usher sync` re-derives the links among them"
        )
    ending = (
        "nothing was committed (--dry-run)"
        if report.dry_run
        else ("committed" if report.committed else "nothing was committed")
    )
    print(
        f"{report.total_written:,} {_unit('row', report.total_written)} written, "
        f"{report.total_present:,} already present, "
        f"{report.total_absent:,} with nothing to write onto, "
        f"{report.total_unresolved:,} skipped as unresolvable, "
        f"{len(report.refused)} refused, from {report.path} "
        f"at schema {report.schema_revision}: {ending}"
    )


def _print_backup_report(report: BackupReport) -> None:
    """One line per table, one summary line, one sentence about the key.

    **Every carried table, zeros included**, for `_print_curation_report`'s
    reason one function down: a table absent from the report and a table
    nobody carries read the same, and at a terminal there is no second export
    to compare against. That matters most for `llm_calls`, which is 0 rows on
    this deployment and is the table PRD 08 calls *"the first thing in this
    project that is not rebuildable from anything, at any price"* -- a spend
    ledger silently dropped would be reported by nothing else.

    The size is `stat()` on the written file rather than a sum of what was
    encoded: gzip's ratio over JSON is the whole reason the format is
    affordable, and a number an operator can check with `ls -l` is worth more
    than one only this command can produce.
    """
    for table, count in report.rows.items():
        print(f"  {table:<24}{count:>9,} {_unit('row', count)}")
    print(
        f"wrote {report.total_rows:,} {_unit('row', report.total_rows)} "
        f"from {len(report.rows)} {_unit('table', len(report.rows))} "
        f"to {report.path} ({report.bytes_written:,} bytes), "
        f"schema {report.schema_revision}"
    )
    print(CREDENTIAL_KEY_WARNING)


#: What POSIX calls an environment variable name, and what `--new-key-env`
#: will accept as one. Anything else is refused **without being printed**:
#: `usher rotate-secret --new-key-env <the key>` is the mistake this exists
#: for, and a base64 key (`+/=`) or a hyphenated one fails here.
_ENVIRONMENT_VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _RefuseAKeyOnTheCommandLine(argparse.Action):
    """`--new-key` exists so that typing it is a refusal rather than a leak."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        # `parser.error`: exit 2 with usage on stderr, the way every other
        # argument failure in this CLI exits. The value is not interpolated.
        parser.error(
            "--new-key is not an argument of this command, and the value after it is not "
            "repeated here because it is probably your new key. A key in argv is in your "
            "shell history and in `ps` output for every user on this box. Export it and "
            "pass the variable's NAME: --new-key-env USHER_NEW_SECRET_KEY"
        )


def _new_secret_key(settings: Settings, variable: str) -> SecretStr:
    """Read the new key out of the environment, and refuse it here or nowhere."""
    if not _ENVIRONMENT_VARIABLE_NAME.fullmatch(variable):
        raise SystemExit(
            "usher rotate-secret: --new-key-env takes the NAME of an exported environment "
            "variable ([A-Za-z_][A-Za-z0-9_]*), and what it was given is not one. It is not "
            "repeated here, because the commonest way to reach this message is to have "
            "passed the key itself -- export the key into a variable and name the variable"
        )
    if isinstance(_key_or_rejection(settings, variable), SecretStr):
        raise SystemExit(
            "usher rotate-secret: what --new-key-env was given is a value this deployment "
            "would accept as a secret key, so it is treated as one and not repeated here. "
            "Pass the NAME of an exported variable instead. (If it really is a variable "
            "name, rename it: a name long enough to be a key cannot be told from one.)"
        )
    raw = os.environ.get(variable, "")
    if not raw:
        raise SystemExit(
            f"usher rotate-secret: ${variable} is not set -- export the new key into it "
            f"(e.g. `export {variable}=$(openssl rand -hex 32)`) rather than passing it "
            "as an argument, and do not add it to .env"
        )
    key = _key_or_rejection(settings, raw)
    if isinstance(key, ValidationError):
        # `settings_rejection`, not `str(exc)`: pydantic renders the rejected
        # *input*, so the naive spelling prints the new secret key at the one
        # command whose entire subject is secret keys.
        raise SystemExit(
            settings_rejection(key, entry_point=f"usher rotate-secret (${variable})")
        ) from key
    return key


def _key_or_rejection(settings: Settings, candidate: str) -> SecretStr | ValidationError:
    """The `SecretStr` `Settings` would hold for `candidate`, or why it would not.

    One construction with two readers, which is what keeps *"is this a key?"*
    and *"is this key acceptable?"* the same question asked twice rather than
    two rules that can drift. `database_url` is passed through so the
    construction cannot fail for a reason that has nothing to do with the key.
    """
    try:
        return Settings(
            database_url=settings.database_url, secret_key=SecretStr(candidate)
        ).secret_key
    except ValidationError as exc:
        return exc


async def _rotate(settings: Settings, *, new_key_env: str) -> None:
    """Re-encrypt every stored credential under a new `USHER_SECRET_KEY`."""
    new_key = _new_secret_key(settings, new_key_env)
    async with _session_for(settings) as session:
        service = RotationService(
            store=PostgresCredentialRotationStore(session),
            old_cipher=build_cipher(settings.secret_key),
            new_cipher=build_cipher(new_key),
            commit=session.commit,
        )
        report = await service.rotate()
    _print_rotation_report(report, new_key_env=new_key_env)
    if report.refused:
        raise SystemExit(_rotation_refusal(report))


def _rotation_refusal(report: RotationReport) -> str:
    """Two diagnoses behind one counter, and only one of them is destructive."""
    count = len(report.refused)
    if count == report.rows:
        return (
            f"usher rotate-secret: all {count} stored "
            f"{_unit('credential', count)} could not be decrypted by either key. That "
            "almost always means the OLD key is wrong rather than that the rows are "
            "corrupt -- nothing was written and no credential was lost. USHER_SECRET_KEY "
            "must still hold the key these rows were encrypted under while this command "
            "runs; changing it first is what produces exactly this result. Put the "
            "previous key back and run this again."
        )
    return (
        f"usher rotate-secret: {count} "
        f"{_unit('credential', count)} could not be decrypted by either key "
        "and must be re-entered -- re-register those sources with "
        "`POST /admin/sources` once the new key is in place"
    )


def _print_rotation_report(report: RotationReport, *, new_key_env: str) -> None:
    """Three counts, the refused refs named, and the sentence about tickets.

    **Refs and no credential**, which is `RotationReport`'s own guarantee
    rather than this function's discretion -- the report has nowhere to carry
    one. The refused refs are named in full and not capped the way
    `_print_restore_report` caps its refusals at `_REFUSALS_NAMED`: that cap
    exists because a restore can refuse 14,166 rows, and this table is one
    row per configured source.

    **The ticket sentence is one line and the command does nothing about
    it.** `services/playback_ticket.py` derives a second subkey from the same
    `USHER_SECRET_KEY`, and rotating invalidates every outstanding ticket --
    which is correct rather than a bug, because a ticket is short-lived and
    never stored, and the alternative is a window in which a superseded key
    still mints working redirects. A client meets it as a `404
    ticket_invalid` and answers by asking `/play` again. Said here because an
    operator watching a dashboard for the next minute should know why.
    """
    print(f"rotated  {len(report.rotated):>4}")
    print(f"already  {len(report.already):>4}")
    print(f"refused  {len(report.refused):>4}")
    for ref in report.refused:
        print(f"  refused: {ref}")
    print(
        f"{report.rows} {_unit('stored credential', report.rows)} considered; "
        f"outstanding playback tickets are invalidated by any key change and "
        f"clients recover by asking /play again"
    )
    if report.rotated:
        print(
            f"set USHER_SECRET_KEY to ${new_key_env}'s value and restart, "
            "or the next start cannot read what this run just wrote"
        )


async def _push(settings: Settings, *, source_name: str | None, probe: bool) -> None:
    """Probe a source's push channel once, or run the lanes in the foreground.

    `--probe` reports the **messages and events that arrived**, never that the
    handshake succeeded: a handshake against a nonexistent path also upgrades
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
    engine = build_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
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
    # The error boundary's own escape hatch, and the reason the boundary is allowed to
    # swallow a stack at all.
    parser.add_argument(
        "--traceback",
        action="store_true",
        help="show the full stack instead of a one-line message",
    )
    # **`action="version"`, and the placement is load-bearing.** `main` parses before it
    # opens the error boundary, so argparse raises `SystemExit(0)` here -- before
    # `get_settings()` runs and before anything reaches a database.
    parser.add_argument(
        "--version",
        action="version",
        version=f"usher {__version__}",
        help="print the version and exit, without reading any settings",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (the default with no arguments)")
    bootstrap = sub.add_parser("bootstrap", help="import bulk catalog datasets")
    bootstrap.add_argument(
        "--phase",
        choices=PHASES,
        default=BootstrapPhase.ALL.value,
        # The ordering warning is on the *option*, not only in the report, because an
        # operator scheduling `credit-names` reads `--help` before they ever see a
        # report -- and getting this order wrong is not recoverable by re-running the
        # phase.
        help=(
            "which bulk datasets to import; a full run walks the steps in the "
            "order listed, and ratings is not one of them -- it re-imports IMDb "
            "ratings alone, which --phase all already does inside imdb. "
            "Run credit-names BEFORE the TMDb enrichment crawl: it defers to TMDb "
            "on every enriched title, so afterwards 99.82%% of the priority tier "
            "never gains IMDb names and re-running does not repair it"
        ),
    )
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

    # `usher work`'s shape exactly, because they are the same bargain one
    # abstraction apart: a daemon form for a deployment, and a `--once` form
    # for an operator's own crontab, which is the supported path for a
    # wall-clock schedule a `ScheduledJob.period` cannot express.
    schedule = sub.add_parser("schedule", help="run scheduled batches whose period has elapsed")
    schedule.add_argument("--once", action="store_true", help="one tick, then exit")

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
    # flight. A number to keep in mind, not an optimum.
    derive.add_argument("--page-size", type=int, default=500)

    genres = sub.add_parser(
        "genres", help="report or normalise the genre vocabulary in titles.genres"
    )
    genres.add_argument(
        "--backfill",
        action="store_true",
        help="rewrite titles.genres into Usher's vocabulary (the bare form only reads)",
    )
    # **An argument rather than a constant**, which is design constraint 3:
    # the right batch is a property of the deployment's `work_mem`, its WAL
    # and how long its operator is willing to hold a transaction, none of
    # which this file knows. 1000 for `index --backfill`'s reason -- a page
    # here carries a uuid and a short `text[]`, not a JSONB payload.
    genres.add_argument("--batch-size", type=int, default=1000, help="rows per transaction")
    genres.add_argument("--limit", type=int, default=0, help="stop after N titles; 0 drains")
    genres.add_argument("--after", help="resume from a title id a previous run printed")

    search = sub.add_parser("search", help="search the catalog")
    search.add_argument("query", help="what to search for")
    # `SearchMode`'s values, taken from the enum rather than retyped: a
    # hand-copied list drifts silently and offers an operator a mode the
    # service cannot serve -- or, worse, omits the fused one.
    search.add_argument(
        "--mode", choices=[mode.value for mode in SearchMode], default=SearchMode.FUSED.value
    )
    search.add_argument("--limit", type=int, default=20)
    # `SearchFilters`' closed vocabulary, one flag per field and no more.
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
    # **Defaults to `fuzzy`, where the route defaults to `prefix`.** A command
    # typed once can afford a cost a keystroke path cannot.
    suggest.add_argument(
        "--tier",
        choices=[tier.value for tier in SuggestTier],
        default=SuggestTier.FUZZY.value,
    )

    evaluate = sub.add_parser("eval", help="measure the quality of a surface against its bars")
    evaluate.add_argument(
        "surface",
        nargs="?",
        default=None,
        choices=["suggest"],
        help="one surface, or every surface when omitted",
    )
    evaluate.add_argument(
        "--full",
        action="store_true",
        help="full golden sets, bars enforced, ledger written (default: a seeded quick sample)",
    )
    evaluate.add_argument(
        "--seed",
        type=int,
        default=GATE_SEED,
        help=f"the golden-set seed (default {GATE_SEED}, ADR-0002's own)",
    )
    evaluate.add_argument(
        "--sample",
        type=int,
        default=100,
        help="cases per surface in quick mode; ignored with --full",
    )

    similar = sub.add_parser(
        "similar", help="how old the neighbour table is, one title's neighbours, or a rebuild"
    )
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
    similar.add_argument(
        "--resume",
        action="store_true",
        help="with --rebuild: start after the last seed already stamped with the running blend",
    )
    similar.add_argument(
        "--max-seeds",
        type=int,
        default=None,
        help="with --rebuild: stop after this many seeds; the rest stay stale for --resume",
    )

    home = sub.add_parser("home", help="compose the home screen, and time it")
    home.add_argument("--limit", type=int, default=10, help="rows to compose")
    home.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="cold compositions to time; the cache is cleared before each",
    )

    # **No arguments at all**, and `--user` is the one deliberately absent: PRD 01
    # leaves authentication as a seam and `usher.db.users` is what stands in it, a
    # singleton `is_default` row.
    sub.add_parser("curate", help="run one LLM generation for the default user")

    push = sub.add_parser("push", help="run the push lane, or probe a source's push channel")
    push.add_argument("--source", default=None, help="source name; omit for every enabled source")
    push.add_argument(
        "--probe",
        action="store_true",
        help="connect, wait, and report what arrived, then exit",
    )

    # The eighteenth subcommand.
    backup = sub.add_parser("backup", help="write everything nothing else can rebuild to one file")
    # `type=Path` rather than `str` plus a conversion in `_dispatch`: argparse is where
    # the surface is described, and a `--output` that is a string here and a `Path`
    # there is two spellings of one argument.
    backup.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write it; default usher-backup-<UTC>.jsonl.gz here",
    )

    restore = sub.add_parser("restore", help="merge one backup artifact into this database")
    # **Positional and required**, unlike `backup --output`, and the asymmetry
    # is the point: a backup with no destination has an obvious default (a
    # timestamped name here), and a restore with no source has none at all --
    # picking the newest file in the working directory is exactly the kind of
    # guess a command that overwrites a household's history must not make.
    restore.add_argument(
        "artifact",
        type=Path,
        help="the .jsonl.gz `usher backup` wrote",
    )
    restore.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve everything, print the identical report, and commit nothing",
    )
    # **`--skip-unresolvable`, not `--allow-unresolvable`**, and the verb is the
    # decision.
    restore.add_argument(
        "--skip-unresolvable",
        action="store_true",
        help=(
            "drop rows naming a title or episode this catalog cannot resolve, "
            "instead of refusing the file; the next `usher sync` re-derives them"
        ),
    )

    rotate = sub.add_parser(
        "rotate-secret",
        help="re-encrypt stored credentials under a new USHER_SECRET_KEY",
        allow_abbrev=False,
    )
    # The tripwire, before the real argument so a reader meets the refusal first.
    rotate.add_argument(
        "--new-key",
        nargs="?",
        action=_RefuseAKeyOnTheCommandLine,
        dest=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    # **A variable name, never a key**, and it is required rather than defaulted.
    rotate.add_argument(
        "--new-key-env",
        required=True,
        metavar="VAR",
        help=(
            "NAME of an exported environment variable holding the new key "
            "(e.g. USHER_NEW_SECRET_KEY) -- the name, not the key; "
            "export it, do not put it in .env"
        ),
    )
    return parser


def _parse_without_echoing_unknown_values(
    parser: argparse.ArgumentParser, argv: list[str]
) -> argparse.Namespace:
    """`parser.parse_args`, without echoing `rotate-secret`'s unrecognised values."""
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if getattr(args, "command", None) == "rotate-secret":
            parser.error(
                f"unrecognized arguments for rotate-secret ({len(unknown)}, not shown -- on "
                "this command an unrecognized value is most likely your new key). The only "
                "way to name a key here is --new-key-env, which takes the NAME of an "
                "exported environment variable"
            )
        # argparse's own wording, kept byte-for-byte so every other command's
        # refusal reads exactly as it did before this function existed. (Its
        # `%`-formatting is spelled as an f-string here only because ruff's
        # UP031 refuses the original.)
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return args


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """`build_parser().parse_args`, plus the cross-argument rules argparse has no vocabulary for.

    A separate function rather than a `build_parser` that validates, so the
    parser stays a pure description of the surface -- and a *public* one
    rather than a private step inside `main`, because the rule below is the
    only thing standing between `--resolve <id>` with no `--title` and an
    `attach_title(title_id=None)` that blanks a link instead of creating
    one, and a rule with no reachable test is a comment.
    """
    parser = build_parser()
    args = _parse_without_echoing_unknown_values(parser, list(argv))
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
    if args.command == "similar":
        # ⚠️ **No arguments is now the whole-table report and no longer an error.** It
        # was refused as "a read of nothing" until M10's J6, which is what issue #17's
        # *"a `usher similar` line that says how old the table is relative to the
        # embedding population"* asks for -- the per-title form already printed two of
        # the three facts and had no whole-table spelling.
        if args.title_id and args.rebuild:
            # `parser.error` again -- exit 2 with usage rather than exit 1 with
            # a traceback.
            parser.error("give a title id, or --rebuild, but not both")
        # The two rebuild-only flags, refused rather than ignored where they
        # cannot mean anything. A `--max-seeds` silently dropped on a read is
        # an operator who believes they capped a walk that never started.
        if not args.rebuild and (args.resume or args.max_seeds is not None):
            parser.error("--resume and --max-seeds need --rebuild")
        if args.max_seeds is not None and args.max_seeds < 1:
            # Zero is not a smaller run, it is a run that reads a page and
            # writes nothing while reporting a successful rebuild.
            parser.error("--max-seeds must be at least 1")
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
    """Pydantic's diagnosis with every rejected value stripped out."""
    return settings_rejection(exc, entry_point=f"usher {command}")


def main(argv: Sequence[str] | None = None) -> None:
    """Every entry point's single door.

    `python -m usher`, the `usher` console script (`[project.scripts]`), and the
    container's `CMD`.
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
    """The command table, lifted out of `main` so one `try` there wraps every arm."""
    if args.command == "bootstrap":
        asyncio.run(_bootstrap(settings, BootstrapPhase(args.phase)))
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
    elif args.command == "schedule":
        asyncio.run(_schedule(settings, once=args.once))
    elif args.command == "index":
        asyncio.run(
            _index(settings, backfill=args.backfill, limit=args.limit, page_size=args.page_size)
        )
    elif args.command == "derive":
        asyncio.run(
            _derive(settings, backfill=args.backfill, limit=args.limit, page_size=args.page_size)
        )
    elif args.command == "genres":
        asyncio.run(
            _genres(
                settings,
                backfill=args.backfill,
                batch_size=args.batch_size,
                limit=args.limit,
                after=_as_uuid(args.after, "title id") if args.after else None,
            )
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
        asyncio.run(_suggest(settings, prefix=args.prefix, limit=args.limit, tier=args.tier))
    elif args.command == "eval":
        asyncio.run(
            _eval(
                settings,
                surface=args.surface,
                full=args.full,
                seed=args.seed,
                sample=args.sample,
            )
        )
    elif args.command == "similar":
        asyncio.run(
            _similar(
                settings,
                title_id=None if args.title_id is None else _as_uuid(args.title_id, "title id"),
                limit=args.limit,
                rebuild=args.rebuild,
                resume=args.resume,
                max_seeds=args.max_seeds,
            )
        )
    elif args.command == "home":
        asyncio.run(_home(settings, limit=args.limit, repeat=args.repeat))
    elif args.command == "curate":
        asyncio.run(_curate(settings))
    elif args.command == "push":
        asyncio.run(_push(settings, source_name=args.source, probe=args.probe))
    elif args.command == "backup":
        asyncio.run(_backup(settings, output=args.output))
    elif args.command == "restore":
        asyncio.run(
            _restore(
                settings,
                artifact=args.artifact,
                dry_run=args.dry_run,
                skip_unresolvable=args.skip_unresolvable,
            )
        )
    elif args.command == "rotate-secret":
        # The **name** crosses this line and the value never does: the
        # environment read is inside `_rotate`, so nothing between argparse
        # and the store ever holds a key in a frame a traceback would print.
        asyncio.run(_rotate(settings, new_key_env=args.new_key_env))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
