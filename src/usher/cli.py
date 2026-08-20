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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

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
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.users import default_user, ensure_default_user
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobRequest
from usher.ports.rows import RowContext
from usher.ports.search import SearchFilters, SearchMode
from usher.ports.source import SourceAdapter
from usher.services.bootstrap import (
    VocabularyState,
    VocabularyVerdict,
    bootstrap_report,
)
from usher.services.curation import CurationReport
from usher.services.curation_validate import DropReason
from usher.services.home import ComposeReport, HomeService
from usher.services.reconcile import RETRACTION_ERROR_CODE
from usher.services.rows import ROW_PROVIDERS, enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RowCache
from usher.services.search import SearchAnswer, SemanticSearchUnavailable, SuggestTier
from usher.telemetry import (
    configure_telemetry,
    register_queue_gauges,
    register_search_gauges,
)

# `--phase all` runs `BootstrapPhase` in order, so the enum *is* the
# execution order an operator reads it as, and three of its edges are
# measured rather than stylistic -- `usher.domain.bootstrap.BootstrapPhase`
# carries the argument and the numbers.
#
# **Derived, never restated.** This was a literal tuple until M9's E5, when
# `POST /admin/bootstrap/{phase}` gave the set a second reader: two
# spellings would let the CLI accept a phase the route rejects, and
# `/openapi.json` would describe a bare string. `argparse` compares a
# `choices=` member with `==`, and a `StrEnum` member equals its own wire
# value, so the tuple is spelled as values and `_dispatch` converts once.
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
#
# **Three of `UsherPortError`'s nine subclasses are here and six are not**, and
# that split is the whole of what M8 added (ADR-0026's Amendment, 2026-08-07).
# `httpx.HTTPError` cannot fire for anything behind a port: an adapter's job is
# to translate its transport's failures *before* they cross, so `httpx` never
# reaches this line from `adapters/llm`, `adapters/emby` or `adapters/tmdb` --
# which left `usher curate` against an unreachable `USHER_LLM_BASE_URL`
# answering with a stack, ADR-0026's own motivating defect in a family it did
# not name.
#
# The line drawn is *reaching* an upstream against everything else. The three
# below are conditions an operator acts on. `RepositoryConflict`,
# `RepositoryNotFound` and `PortDataMalformed` stay out because several of
# their raise sites are deliberate tripwires for bugs in this project's own
# code (`title_neighbors`' bounds, the credits delete's scope, a curated batch
# this project assembled wrong), and a one-line message is exactly what those
# must not become. `SourceNotSupported`, `FilterNotSupported` and
# `AvailabilitySweepRefused` -- the three that live beside their own port
# rather than in `ports/errors.py`, which is why nobody counts them -- stay out
# for the opposite reason: no measured path reaches this boundary with one, and
# ADR-0026 asks for evidence per family before the tuple grows.
#
# ⚠️ **`AvailabilitySweepRefused` now has that evidence and still stays out, and
# the two reasons are worth keeping apart** (M10 S9, 2026-08-19). The *family*
# does occur in the field: the operator's own `sync_runs` holds a `full` run
# refused by ADR-0015's ceiling on 2026-08-13. What it does not have is a
# **path** here -- `ReconcileService.reconcile` absorbs it into a `FAILED` row
# and its docstring promises never to raise, deliberately, so one source's
# refusal cannot abort a multi-source sync. Adding it to this tuple would be a
# decision with no effect. `_sync` reports it off the **run row** instead, which
# is the artefact that does cross this boundary, and exits non-zero there.
# *"Unreachable here"* and *"never observed"* are two claims, and only the first
# is still true of this one.
#
# `tests/unit/test_cli_errors.py::
# test_the_port_taxonomy_is_split_and_the_base_class_is_not_in_the_tuple`
# reads the set off `__subclasses__()`, so a tenth member cannot arrive
# without a decision about it.
OPERATOR_ERRORS: tuple[type[Exception], ...] = (
    # A refused connection, a name that does not resolve, a full disk, a
    # bulk dataset that is not where it was left.
    OSError,
    # Everything the driver does wrap: a missing table (`alembic upgrade
    # head` never ran), a dead pool, a permission the role does not have.
    SQLAlchemyError,
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
    # The credential was rejected. `USHER_LLM_API_KEY`, `USHER_TMDB_API_KEY`,
    # a source's stored password -- an operator fixes all three, and none of
    # them is worth sixty frames.
    PortAuthFailed,
    # The upstream asked to be backed off. A CLI has no backoff schedule to
    # apply, so the honest answer at a terminal is the sentence and exit 1.
    PortRateLimited,
)
# 128 + SIGINT, the shell's convention, so a wrapping script can tell an
# operator's Ctrl-C from a command that failed.
_INTERRUPTED_EXIT_CODE = 130


async def _bootstrap(settings: Settings, phase: BootstrapPhase) -> None:
    """One command's session and engine, wrapped around the dispatch both
    roots share.

    **The phases themselves are `composition.run_bootstrap`'s** since M9's E5,
    because `POST /admin/bootstrap/{phase}` needs the same seven arms in the
    same order and a handler that re-implemented them would be a second
    dispatch that drifts. What stays here is what a *command* owns and a
    worker does not: an engine of its own, one session for the run, and
    `print` as the report sink.

    The engine is disposed in a `finally` for the reason it always was -- a
    phase that raises must still give its connection pool back -- and the
    client's own `finally` is one layer down, in `run_bootstrap`, where the
    client is built.

    **`NullEventPublisher()` is a real deployment, not a test double.** M5's
    bus is in-process, so a `bootstrap.progress` frame raised in *this*
    process has no SSE client on the other side of it -- the same answer
    `usher work` has given for `title.updated` since M5, and the same
    degradation PRD 07 and PRD 08 record for a split deployment. A client
    that wants these frames watches the server that ran the phase.
    """
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
    """Walk each selected source: items first, then watch state.

    The two lanes are one command because they are one operator intention
    ("bring this server up to date") and because the item walk has to run
    first -- `WatchStateSyncService` resolves each state against a
    `MediaItem`, so a watch lane that ran before the items existed would
    count every state unmatched and merge nothing.

    **One pipeline for the whole command, which is also one outbound gate per
    source for the whole command** (ADR-0039 §4). `build_pipeline` builds a
    `SourceGateRegistry` when nobody hands it one, and this command opens
    exactly one pipeline and loops the sources inside it -- so the walk and the
    watch lane below share a gate per source, and two sources get two.

    ⚠️ **Until 2026-08-19 this paragraph ended *"a second `build_pipeline` here
    would be a second registry and twice the rate"*, and that is unreachable as
    written.** Doubling the rate for one source takes **two** things -- two
    registries *and* two adapters built against them -- and this command has
    one of each, so either one alone is a sufficient guard. The loop below
    opens **one** adapter per source via `_open_adapter` and hands that same
    object to the reconcile walk and to the watch lane, so however many
    pipelines existed there would still be one gate per source. Measured: both
    spellings of the claim's own defect -- a second `build_pipeline` beside the
    first, and one moved inside the loop -- survive as equivalent mutants,
    because the adapter count is what is really holding it. The load-bearing
    property is therefore **one `_open_adapter` per source**, which is what
    `tests/unit/test_composition.py::test_the_cli_roots_compose_once_rather_than_per_scope`
    asserts, alongside the property that *is* about the registry: the
    `build_pipeline` call is in this function's own body and not inside a
    closure that runs per scope.

    🔴 **A run that recorded `FAILED` exits non-zero, and until 2026-08-19 it
    did not.** `ReconcileService.reconcile` absorbs every `UsherPortError`
    into a `FAILED` row and never raises -- deliberately, so one source's
    failure cannot abort a multi-source sync -- so nothing reached `main`'s
    boundary and the command exited 0 having printed the word `failed`. A
    human reading the terminal saw it; cron, CI and a systemd unit did not.
    Measured on the deployment this milestone was written against: one `full`
    run refused by ADR-0015's ceiling on 2026-08-13, and **ten consecutive
    `watch_state` failures with not one completion, every one of them exit 0.**

    **The exit is collected and raised after the loop**, never inside it: the
    reason `reconcile` swallows the exception in the first place is that the
    remaining sources still have to be walked, and exiting early would
    reintroduce exactly that.
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
    without reading. The token is matched rather than the refusal's English,
    because that sentence is built from three numbers in `ports/ingest.py` and
    is a standing candidate for rewording.
    """
    lanes = ", ".join(f"{one.kind.value}" for one in runs)
    line = f"{len(runs)} sync run(s) failed: {lanes}; see the lines above and `usher sync-status`"
    if any(RETRACTION_ERROR_CODE in (one.error or "") for one in runs):
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

    **The same argument, one upstream over, and it is why `unit_of_work` is
    built once here rather than per job.** The daemon below opens a scope per
    claim and per job, so anything that lives on a `Pipeline` lives for one
    job -- including, before M10's S3, the outbound gate on every source
    adapter. `unit_of_work` now resolves a `SourceGateRegistry` once and closes
    over it, so this process paces one source at
    `USHER_SOURCE_REQUESTS_PER_SECOND` however many jobs are in flight
    (ADR-0039 §4). **A second `usher work` container is a second registry and
    therefore twice the rate** -- a capacity decision an operator makes, and one
    nothing in a process can make for them.

    **Publishes to `NullEventPublisher`, and that is a stated consequence
    rather than an oversight.** `usher work` is a separate process and M5's
    bus is in-memory, so an enrichment finished here reaches no SSE client;
    a client that refetches still gets the enriched title, which is PRD 08's
    own degradation rather than breakage. The server process runs the same
    worker as a lane (`usher.api.lanes`) so PRD 03's read-through loop
    closes there, and `EventPublisher` is a port precisely so the fix for
    the split deployment is a second implementation rather than a branch.
    """
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

        # 🔴 **`-inf`, never `0.0`** -- `api/lanes.py` carries the argument, and
        # it applies here with one extra consequence: `usher work --once` from
        # a cron inside the first 150 s of host uptime would recover nothing at
        # all, while `_measure` below claims it recovers "before the first
        # claim". `time.monotonic()` is seconds since boot on Linux.
        throttled_at = float("-inf")
        # The running total of what this process has taken back from workers
        # that stopped heartbeating, kept for the same reason the server keeps
        # it in `/health/ready`'s body: `recover()` has returned this number
        # since M9's W1 and **both** callers discarded it, so the only trace of
        # M9's S3 condition was a WARNING that fires when the count is
        # non-zero. This command has no readiness route, so it goes in the pass
        # line it already prints rather than growing a surface (M10 F2).
        #
        # An `int` here and an `int | None` on `LaneReport`, deliberately.
        # `None` there is *"this process runs no worker"*, which is a state
        # `create_app` really has (`USHER_WORKER_ENABLED=false`) and which
        # `usher work` cannot be: with the origin above, the first `_measure`
        # always recovers before anything is printed, so `0` on this line means
        # *asked and found none* and there is no third state for a `None` to
        # name. Spelling it `int | None` would add a branch no run can reach.
        recovered = 0

        async def _measure() -> int:
            # PRD 08's recovery, on the lease rather than on "everything
            # running". Before the first claim, so a dead process's abandoned
            # claims are this one's work rather than nobody's -- and it is now
            # safe to run beside another live worker, which is the whole
            # difference from the `startup()` it replaces. Throttled to half
            # the lease for `api/lanes.py`'s reason: it is an `UPDATE` scanning
            # `status = 'running'`, and between leases there is nothing to
            # find.
            nonlocal throttled_at, recovered
            now = time.monotonic()
            if now - throttled_at >= settings.job_lease_seconds / 2:
                recovered += await worker.recover()
                throttled_at = now
            done = await worker.run_once()
            async with work() as pipeline:
                await gauges.refresh(pipeline.queue)
                await backlog.refresh(
                    pipeline.embeddings, pipeline.neighbors, settings.embedding_model
                )
            return done

        ran = await _measure()
        print(f"{ran} jobs, {recovered} recovered claims")
        while not once:
            if ran == 0:
                await asyncio.sleep(_IDLE_SLEEP_SECONDS)
            taken = recovered
            ran = await _measure()
            if recovered != taken:
                # **On a change, never per pass.** Without this a daemon prints
                # exactly one line, at startup, when the total is almost always
                # zero -- so every later recovery, which is precisely M9's S3,
                # is invisible in the only mode a container runs, and PRD 08's
                # "`usher work` ... prints the same total in its pass line"
                # would be true of `--once` alone. A line *per pass* is the
                # other error: at `_IDLE_SLEEP_SECONDS` that is ~17,280 a day,
                # the rate `.claude/rules/config-cli-and-deployment.md` already
                # records as training an operator to ignore output.
                print(f"{ran} jobs, {recovered} recovered claims")
    finally:
        await registry.aclose()
        await aclose()
        await aclose_model()
        await aclose_client()
        await engine.dispose()


async def _derive(settings: Settings, *, backfill: bool, limit: int, page_size: int) -> None:
    """Report derivation coverage, or re-derive people, credits, collections
    and artwork inline.

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
        # **Expect this to be small against a large cache, and that is not a
        # defect**: `images` joined `*_APPEND_TO_RESPONSE` in M4, so a payload
        # cached before then derives only its two top-level primaries.
        print(f"images written: {report.images_written:,}")


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

    **This command writes a `search_queries` row and it is the root that
    proves the commit is the service's.** `_session_for` yields a session and
    disposes the engine **without ever committing**, so a row left for the
    caller would be rolled back here and nowhere else -- `api/deps.get_session`
    commits when a handler returns and would have hidden it. Nothing below
    commits; `SearchService` does (F2).

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

    **The completion client is built only when a rewrite could actually be
    bought, and that condition has three parts because the cost has three ways
    of being wasted.** Query expansion sits in front of the embed, so: a mode
    with no embed has no call to put in front of one; a deployment whose
    embedder did not load has no embed either, and `SearchService` narrows to
    full-text before it ever reaches an expander; and
    `USHER_QUERY_EXPANSION_ENABLED` is `false` by default even where the LLM is
    on, so `build_pipeline` would decline to build the expander anyway. In each
    of the three an `httpx.AsyncClient` and its pool would be opened and closed
    for nothing -- which is verbatim the cost the `full_text` guard exists for,
    and the middle one was live until 2026-08-07: `embedder(...)` answering
    `(None, nothing)` on the line above did not stop the client below it.

    **Spelled as two conjuncts rather than three**, because `model` is built
    only for a non-`full_text` mode, so `model is not None` already answers the
    first part -- and a third clause restating it would be a condition no
    configuration can make false on its own, i.e. exactly the unobservable code
    this project keeps finding in mutation sweeps.

    The pair reads as one question -- *is there an embed for a completion to
    sit in front of, and does this deployment want one?* -- and it mirrors
    `build_pipeline`'s `llm is None or not settings.query_expansion_enabled`
    rather than duplicating it: that decides whether the *service* exists, this
    decides whether the *pool* is opened, and only this side can be asked
    before a pipeline exists.
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
    # After the embedder, and reading its answer: `model is None` is the
    # narrowed deployment, and there is nothing to expand for. `llm_client` is
    # pure construction and cannot raise, `embedder` is the one that can, so
    # this order also keeps a failed model load from leaking a pool.
    # `report=False` for the reason above -- that factory's line is *"curate
    # jobs will not be claimed"*, which is about a lane this process does not
    # run. `query_expansion_enabled` implies `llm_enabled` (`config.py` refuses
    # the other pairing), so the switch is asked about once here.
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
                    # `ensure_default_user`, not `default_user`: this command
                    # needs an id and nothing else, exactly as `usher curate`
                    # does, and PRD 01's authentication seam is a singleton row
                    # until a request has one to carry.
                    #
                    # **Not committed *here*, and since F2 that is no longer
                    # the same as "not committed".** This command commits
                    # nothing of its own -- `_session_for` yields a session and
                    # disposes the engine -- but `SearchService` now writes a
                    # `search_queries` row and commits it, and the household
                    # row is in that same transaction. So on a first run the
                    # user this line created lands durably, which is what makes
                    # the row's `NOT NULL` foreign key satisfiable at all: the
                    # analytics write is the only writer on this path, and a
                    # commit that carried the row without its household would
                    # be refused rather than silently partial.
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
    """The operator's answer. `print`, never `logger` -- `_print_home_report`'s
    and `_print_curation_report`'s split, and the same reason: a command's
    answer is stdout.

    A function of its own rather than a tail of `_search`, for
    `_print_curation_report`'s reason: everything above it needs a database and
    everything here needs a `SearchAnswer`, and the expanded-query line is the
    one report in this milestone whose *absence* is the defect.
    """
    if answer.expanded_query is not None:
        # **Before the results, because it is the question they answer.**
        # Reported on every search that bought a rewrite, not only when it
        # looks surprising: a viewer who searched for one thing and got results
        # for another cannot tell a good expansion from a bad one without
        # seeing it, and neither can an operator reading their bug report.
        # `expanded_query` is `None` on every path that embedded the query as
        # typed, so this line never appears on a deployment with expansion off.
        # **It is not a spend report and must not be read as one**: a call that
        # answered with the wrong key is billed in full and still leaves this
        # `None`, so no line here means the query was embedded as typed, never
        # that nothing was bought. `llm_calls` is where spend is legible.
        print(f"expanded: {answer.expanded_query}")
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


async def _suggest(settings: Settings, *, prefix: str, limit: int, tier: str) -> None:
    """Type-ahead, at a terminal, from whichever of the two tiers is asked for.

    **No embedder in either direction.** `SuggestIndex` is its own port
    (🔶 2) and neither implementation loads a model -- one queries `titles`
    through a trigram index and the other through a btree, and both write
    nothing -- so this command starts in 0.13 s on any deployment, including
    one with no embedding extra installed at all, which is PRD 05's
    catalog-lookup tier serving all 1.27M titles with no model.

    No coverage line, and that is not an omission: there is no semantic lane
    here to have degraded.

    **`--tier` defaults to `fuzzy` where `GET /search/suggest?tier=` defaults
    to `prefix`, and the two defaults disagree on purpose** (ADR-0031). This
    command has been the typo-tolerant one since M6 and CLAUDE.md documents it
    as such; a route is driven per keystroke and a command is typed once, so
    the number that decides the route's default -- 2,707 ms p95 at one
    character -- is a cost this caller pays once and can afford. Making them
    agree would have to break one of the two, and `SearchService.suggest`
    therefore takes `tier` as a **required keyword with no default at all**, so
    neither boundary can inherit the other's answer by accident.

    **And no minimum prefix length here, for the same reason.** The route
    refuses tier 1 below four characters because a keystroke path cannot afford
    the short end of B3's curve; refusing it here would take a capability away
    from the one caller that can, and diagnosing tier 1 at one character is
    exactly what an operator would open this command to do.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        results = await pipeline.search.suggest(prefix, limit=limit, tier=SuggestTier(tier))
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
            # path that reads the vectors.** PRD 05 promised "~7%" since
            # before an importer existed and never said of what; these are the
            # two numbers that answer it, and the second is the one that
            # decided whether the term could promote anything.
            #
            # **Past tense, since M9's S7: the genome is no longer a term in
            # the blend** -- 2.4746% of candidate pairs carried one on both
            # sides over an enriched population, against the 10% floor the 0.25
            # weight assumed, so the weight came out and the *measurement*
            # stayed. This report is now the only consumer of the pair read,
            # and it is what a later milestone would re-open the decision on,
            # which is why the wording says a pair *carried* a vector rather
            # than that it scored anything.
            #
            # The pair rate is *measured*, never squared: genome membership
            # and candidate-pool membership both correlate with popularity and
            # with enrichment, so `coverage ** 2` is wrong in an unknown
            # direction -- S5 measured the correction factor at **1.75x** for
            # the genome over 13,064,700 pairs.
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
    mean nothing. The warm read is timed once, separately, and labelled.

    **The two numbers still mean what they meant before M9 added serve-stale**,
    and that is a property of the composer this command builds rather than of
    the arithmetic below: it passes no refresher, so its screen cache is
    fresh-or-miss exactly as it was in M7. See the call site.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        user = await default_user(session)
        # The same wiring `api/deps.py` builds per request, minus the request:
        # `taste` and `affinities` are values the composer hands over, because
        # a provider may import only `domain/` and `ports/`.
        #
        # **`affinities` is a callable here for the reason it is one there**
        # (`ports/rows.py` argues it): the read behind it is three statements,
        # and only `GenreAffinityProvider` awaits them.
        #
        # One consequence is specific to this command and worth naming, since
        # `--repeat` exists to produce a number somebody quotes. The affinity
        # read used to happen *once*, before the timed loop, so no repeat paid
        # for it; it is now inside every run that reaches the provider. Two of
        # its three statements -- `list_recent` and the library-wide genre
        # aggregate -- are memoised on `TasteService`, which is one object for
        # this whole command, so run 1 pays them and runs 2..N do not; the
        # `list_by_ids` over the window is paid by each. Deliberately *not*
        # wrapped in the route's per-request memo (`api/deps.py:_Affinities`):
        # a repeat that skipped the read entirely would report a cold compose
        # that never happens on the route.
        #
        # No lambda-in-a-loop hazard: this closes over `pipeline` and `user`,
        # both bound once above.
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
        # **The same table `GET /home` filters against, read by the same join.**
        # A setting honoured by one composition root and not the other is two
        # different products, and this is the root an operator reaches for when
        # a shelf is missing -- so it must not be the one that still shows it.
        # Both halves come out of one read: the providers to compose, and the
        # slugs to report as switched off.
        provider_settings = row_provider_settings(
            await pipeline.row_provider_settings.overrides(), pipeline.row_providers
        )
        disabled = [one.slug for one in provider_settings if not one.enabled]
        # **No refresher, and the `None` is the decision rather than an
        # omission.** `HomeService` gates its stale-serve grace window on
        # having one, so this composer is M7's fresh-or-miss cache exactly as
        # before -- which is what keeps the cold/warm pair below meaning what
        # it has always meant.
        #
        # A refresher here would have nothing to run it: the process ends when
        # the command does, so a scheduled refresh is a task cancelled
        # mid-flight, and `GET /home`'s lane lives in a server this command is
        # not. A *no-op* refresher would be worse than none -- it would open
        # the grace window with nothing behind it, so a screen 31 s old would
        # be served stale and never replaced, which is the one state PRD 06's
        # sentence must not produce.
        service = HomeService(
            enabled_row_providers(provider_settings), cache=cache, refresh=None, max_rows=limit
        )

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

        _print_home_report(report, cold=cold, warm=warm, disabled=disabled)


def _print_home_report(
    report: ComposeReport, *, cold: Sequence[float], warm: float, disabled: Sequence[str]
) -> None:
    """The operator's table. `print`, never `logger` -- the split every command
    in this module makes: loguru output is operational and goes to a sink an
    operator may not be reading, and a command's answer is stdout, which is
    what gets piped.

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
    """One generation for the default household, at a terminal.

    **Ships alongside `POST /admin/rows/regenerate` and `JobKind.CURATE`
    rather than instead of them**, which is `usher home`'s relationship to
    `GET /home`: the route promises a 202 and says nothing about when the
    work runs, and the job is claimed by whichever worker has a client. This
    command is the one surface where an operator gets the *answer* -- what
    the pool was, what survived, what it cost -- in the same breath as the
    request. PRD 06's "one modest completion per user per day" is a budget
    this command spends one of, so it prints what it bought.

    ## The disabled deployment answers before anything is opened

    **There is no `CurationService` to build.** `composition.llm_client`
    answers `(None, no-op)` for `USHER_LLM_ENABLED=false` and
    `CurationService` spells its client `LLMClient`, never
    `LLMClient | None`, so "no client, no curation" is a `mypy` fact at the
    composition root rather than a branch inside the service. Every other
    surface in this milestone degrades around that: `GET /home` is a shorter
    screen because nine of ten providers need no model, and `usher work`
    keeps five job kinds because `build_worker` registers `CURATE` under the
    same guard `INDEX` sits behind. **This command has exactly one job**, so
    there is nothing to narrow to, and a run that printed an empty report
    and exited 0 would tell a cron entry that curation is running.

    So it is `SystemExit` with a sentence -- the convention `_as_uuid`, the
    semantic-search guard and `similar`'s cross-argument rule already use,
    and which `main`'s boundary passes through untouched because
    `SystemExit` is a `BaseException`. Not a new exception type and not a
    second handler: a deployment configured without a model has not
    *failed*, it said so once, at startup.

    `report=False` for `usher search`'s reason. `llm_client`'s own warning is
    *"curate jobs will not be claimed"*, which is right for `usher work` and
    wrong twice over here -- this process claims no jobs, and
    `_print_home_report`'s printed-not-logged rule would put a JSON envelope
    in front of the answer.
    The sentence below names the two settings instead, which is better
    information rather than the same information.

    **It took a second fix for that to be true of the run that succeeds**, and
    the first one was defending an outcome it could not deliver. `report=False`
    silences Usher's own line; `httpx` was writing one of its own, at INFO,
    once per request, through `_InterceptHandler` and onto the same stdout --
    so on the shipped defaults the report opened with a ~900-character JSON
    envelope about its own completion. Measured 2026-08-07, quieted in
    `configure_telemetry`, pinned by
    `test_httpxs_per_request_info_line_does_not_reach_the_sink`. Worth stating
    here because the equivalent case for this command cannot catch it: the
    integration fixture substitutes `FakeLLMClient`, which opens no socket, so
    a `sink == []` assertion over it would be green against a shipped path
    that logs.

    ## The two conditions that raise, and why one arm covers both

    `generate()` raises `PortDataMalformed` for an **empty candidate pool**
    (PRD 08's "every command works against an empty database"; the one path
    that attempts no call and so writes no `llm_calls` row) and for a
    **generation that validated to zero rows** (ADR-0028's rule 3, carrying
    `CurationRejected.error`, which is numbers and label names only). The
    adapter raises the same type for a completion this endpoint could not
    produce -- a truncated answer, a schema it will not accept, a prompt over
    the context length -- and every one of those messages is a written
    sentence that names its own fix.

    **That family is exactly the one `JobWorker` parks**: retrying does not
    help, so a human has to act, which is ADR-0026's own test for what an
    operator-facing message is. The CLI's equivalent of parking is a sentence
    and exit 1. Everything else keeps its stack, exactly as `OPERATOR_ERRORS`
    leaves everything it does not name.

    **An endpoint that is down, rate-limiting or refusing the key is not this
    arm's**, and since ADR-0026's 2026-08-07 amendment it is not a stack
    either -- `PortUnavailable`, `PortRateLimited` and `PortAuthFailed` are in
    `OPERATOR_ERRORS`, so `main`'s one boundary answers them, one layer out,
    with the same sentence-and-exit-1 every other command gets. This arm is
    deliberately not widened to meet them: the boundary already has the
    families whose fix is "start it, wait, fix the key", and a second handler
    here would be the per-command shape ADR-0026 exists to refuse. It costs
    them the screen clause below, which is the honest trade -- `replace_for_user`
    is unreached on those paths too, but the message an operator needs first is
    the endpoint's.

    **The arm does not branch on which of the three it was**, and that is a
    decision rather than an omission. The service's own message is the
    diagnosis in each case and they read nothing alike; a CLI that wanted to
    add a per-case next step would have to tell them apart by sniffing the
    message or by reading `PortDataMalformed.detail`, which is coupling to a
    field whose documented job is naming an offending record. What the arm
    *does* add is the one fact none of the three messages carries and every
    operator asks first: **last night's screen still stands** -- PRD 08's
    degradation row, true on all three paths because `replace_for_user` is
    reached on exactly one.

    **It says that and not "nothing was written", and the difference is the
    money.** Only the empty pool attempts no call. The other two reach
    `CurationService._settle`, which writes a **committed** `llm_calls` row
    with `ok = false` and the real token counts -- deliberately, because a
    failure with zeroed tokens is indistinguishable from a call that never
    happened. So "nothing was written" was false on two of the three paths,
    and false in the direction that matters: on a generation that validated
    to zero rows the operator has been *charged*, which is the exact state
    ADR-0028's rule 3 exists to make visible. This command's own integration
    case asserts the contradiction --
    `test_curate_says_what_it_dropped_when_nothing_survived` requires
    `len(ledger) == 1`, "the call was billed and the ledger has to say so".
    The screen is what this sentence is about; the spend is what `llm_calls`
    is for, and the tokens and cost this command prints on the path that
    succeeds.

    `--traceback` does not reopen it, for `_settings_problem`'s reason
    rather than its own: these stacks are this project's own frames raising
    a message that is already complete, so re-raising adds lines and no
    diagnosis.
    """
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
    """The operator's answer. `print`, never `logger` -- `_print_home_report`'s
    split, and the same reason: a command's answer is stdout.

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
    bootstrap.add_argument(
        "--phase",
        choices=PHASES,
        default=BootstrapPhase.ALL.value,
        # The ordering warning is on the *option*, not only in the report,
        # because an operator scheduling `credit-names` reads `--help` before
        # they ever see a report -- and getting this order wrong is not
        # recoverable by re-running the phase. `PHASES` is `BootstrapPhase`'s
        # own order and that enum's docstring carries the measurement.
        #
        # `%%`, not `%`: argparse interpolates a help string against its own
        # parameter dict, so a bare `%` raises `TypeError` from `--help` and
        # from nothing else. Found by running it -- ruff, mypy and every
        # existing case pass against the broken spelling, because none of them
        # renders help.
        help=(
            "which bulk datasets to import; the choices are in execution order. "
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
    # **Defaults to `fuzzy`, where the route defaults to `prefix`.** Stated in
    # `_suggest`'s docstring and in ADR-0031: this command has been the
    # typo-tolerant one since M6, and a command typed once can afford a cost a
    # keystroke path cannot.
    suggest.add_argument(
        "--tier",
        choices=[tier.value for tier in SuggestTier],
        default=SuggestTier.FUZZY.value,
    )

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

    # **No arguments at all**, and `--user` is the one deliberately absent:
    # PRD 01 leaves authentication as a seam and `usher.db.users` is what
    # stands in it, a singleton `is_default` row. A flag naming a household
    # would be an id an operator has no way to look up on a deployment that
    # has exactly one -- it lands with the request that carries a user, which
    # is M9's.
    sub.add_parser("curate", help="run one LLM generation for the default user")

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

    **The rendering moved to `usher.config.settings_rejection` on 2026-08-13
    and this is now the CLI's name for it.** It had to move because
    `alembic upgrade head` was leaking the same way and an import-linter
    contract forbids anything importing `usher.cli` -- so the control could
    not be reached from the second entry point that needed it. The evidence,
    the failure it prevents and why `msg` is scrubbed as well as `input`
    dropped all live on that function now; this wrapper exists so the CLI's
    prefix stays `usher <command>:` and so every caller here keeps one name.
    """
    return settings_rejection(exc, entry_point=f"usher {command}")


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

    **`_sync`'s failed-run exit is not a hole in that boundary.** A `SyncRun`
    that recorded `FAILED` is a *value* the command was handed, not an
    exception it caught -- `ReconcileService.reconcile` absorbed the exception
    three layers down and promises to, so there is nothing here to translate.
    It exits through `SystemExit` like the five below.

    Reading the settings is inside it too. A `.env` that fails validation is
    the same kind of failure as a database that is down, it reaches the
    operator through the same command, and it is the case that was leaking a
    credential (see `_settings_problem`).

    `SystemExit` is untouched by all of it: it is a `BaseException`, the
    handlers below name only `Exception` subclasses, and five places in
    this module already exit with a message chosen for the failure it
    describes -- `_as_uuid`, the semantic-search guard, `similar`'s
    cross-argument rule, and both of `curate`'s (no LLM configured, and a
    generation that did not happen).
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
        asyncio.run(_suggest(settings, prefix=args.prefix, limit=args.limit, tier=args.tier))
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
    elif args.command == "curate":
        asyncio.run(_curate(settings))
    elif args.command == "push":
        asyncio.run(_push(settings, source_name=args.source, probe=args.probe))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
