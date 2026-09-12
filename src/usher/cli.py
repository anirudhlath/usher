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
from loguru import logger
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
# `BootstrapPhase` that are *steps* -- and three of its edges are measured
# rather than stylistic; `usher.domain.bootstrap.BootstrapPhase` carries the
# argument and the numbers.
#
# ⚠️ **The enum is not the execution order, and this comment said it was until
# ADR-0040.** Two members are aliases rather than steps (`PHASE_ALIASES`):
# `all`, and `ratings`, which is declared immediately after `imdb` because
# that is the phase whose second half it re-runs. `PHASES` is derived from the
# enum, so `--help` lists `ratings` second -- and the sentence this replaces
# told an operator reading that list that `--phase all` therefore runs it
# second. It does not dispatch it at all; a full run reaches those rows inside
# the `imdb` arm. The steps' own order is still the measured one and
# `tests/unit/test_composition.py` asserts the enum's declaration order and
# `FULL_SEQUENCE` agree, so this list cannot advertise an order the dispatch
# does not run.
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
    #
    # **`DBAPIError`, not `SQLAlchemyError`, and the narrowing is issue #8's
    # measured half.** This line read `SQLAlchemyError` until 2026-08-19, and
    # `SQLAlchemyError` is also the base of `InvalidRequestError` -- which is
    # `MissingGreenlet`, `PendingRollbackError`, `ObjectDeletedError`,
    # `ArgumentError`, `CompileError`: every one of them a bug in this project
    # rather than a condition an operator can act on. M9's S3 measured the
    # cost. One of three `usher work` daemons died 78 minutes into a
    # 130,334-request enrichment crawl on an unhandled `MissingGreenlet`, and
    # the entire record it left in `w1.log` was the two lines
    # `_operator_problem` prints. The stack that would have diagnosed it was
    # caught here and discarded, and the issue was filed reading "the run used
    # bare `usher work`, so no stack was recorded" -- which put the fault on
    # the operator for not passing `--traceback` when the fault was this
    # tuple. `DBAPIError` is what the comment above already claims to admit:
    # errors *the driver raised*, which is where a missing table, a dead pool
    # and a rejected permission all arrive.
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
    # The credential was rejected. `USHER_LLM_API_KEY`, `USHER_TMDB_API_KEY`,
    # a source's stored password -- an operator fixes all three, and none of
    # them is worth forty frames. (*"Sixty"* here and in three other places
    # until 2026-08-20, when M10's F4 measured the only one of them anybody
    # had ever counted: 40 at a real terminal, and the 60 was a pytest run's
    # 62 with 25 harness frames in it. This line predates F4 and is amended
    # with it, because a census restated in four places goes stale in three.)
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
    source for the whole command** (ADR-0043 §4). `build_pipeline` builds a
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

    **`--title` has three bad values and each gets its own sentence, which is
    why the title is read before anything is written** (issue #5, fixed on
    `main` and again as M10's F4; `main`'s spelling is the one that shipped).
    A value that is not a UUID is `_as_uuid`'s; a `--resolve` naming no media
    item is `attach_title`'s `rowcount == 0` -- the `UPDATE` matches nothing,
    so nothing is written and there is nothing to undo; and a well-formed
    UUID naming no title used to be `fk_media_items_title_id_titles`,
    translated to `RepositoryConflict` whose message names the *media item*,
    the id that was fine. That family is deliberately outside
    `OPERATOR_ERRORS`, because its other raise sites are tripwires for bugs in
    this project's own code, so `main` re-raised it and an operator got
    **40 frames** for pasting the wrong column of the listing above, which
    prints a media item id and no title id at all. (Measured 2026-08-20 by
    running the real console script against a throwaway
    `pgvector/pgvector:pg17`: 40 `File` lines over four chained tracebacks, 35
    of them library code, 5 in this project. This docstring and PRD 09 both
    said *"sixty"* until then, which was the pytest run's 62 -- and 25 of
    those are `_pytest`/`pluggy`/`pytest_asyncio` frames no operator ever
    sees. See `.claude/rules/mutation-sweeps.md`.)

    **The lookup is here rather than in `OPERATOR_ERRORS`, and that is the
    argued half.** Adding `RepositoryConflict` to the tuple is the one-line
    version, and every raise site of that family was read before it was
    refused: this is the **only** one an operator's own CLI argument reaches.
    ADR-0026's bar is *"a family belongs in the tuple when an operator can act
    on it"*, and one site out of that census is not a family. **ADR-0026's
    Consequences is the authority for the census**; `09-roadmap.md`'s
    discharged debt entry and
    `test_the_port_taxonomy_is_split_and_the_base_class_is_not_in_the_tuple`'s
    assertion message restate it, and those three are the only copies -- kept
    deliberately few, because a grep-checkable number is exactly what goes
    stale one place at a time.

    **Not `except RepositoryConflict` around the write either**, which reads
    identically to an operator and is not the same thing: Postgres refuses
    the row *after* the statement has run, inside a SAVEPOINT this command
    would then have to unwind, and the same handler would swallow the other
    conflicts `attach_title` can raise. `POST /admin/unmatched/{id}/resolve`
    has read the title first since M9's E4 for the same reason, and its own
    docstring states the rule: everything the request names is checked before
    anything is written.

    Both refusals print and return rather than raising `SystemExit` the way
    `_as_uuid` does. One command naming two things that do not exist owes
    them one exit code, and `no such media item` has had this one since M4.

    ⚠️ **The read and the write are not one statement, so this is a
    check-then-act race, and the residual is accepted rather than absent.** A
    title deleted between `titles.get` and `attach_title` hands the operator
    back exactly the stack #5 was filed about. What bounds it is that
    **nothing in this project ever deletes a `titles` row**: `src/usher/`
    holds **12** `DELETE FROM` statements (measured 2026-08-20) and they name
    `title_embeddings`, `title_neighbors`, `title_search_names`, `credits`,
    `genome_tags`, `curated_rows`, `images` and `jobs` -- never `titles`
    itself, and there is no ORM-level delete either. So the window needs an
    out-of-band `psql`, a restore, or a second process doing something this
    codebase has no path for, and when it fires it degrades to the *pre-fix*
    behaviour rather than to anything worse -- no wrong row is written,
    because the foreign key is still there underneath. Closing it would mean
    `SELECT ... FOR SHARE` on a row this command holds no other reason to
    lock, on the read path of a hand resolution, to defend against a delete
    that has no caller. Stated rather than left as an unspoken *"this cannot
    happen"*, which `.claude/rules/ports-and-error-taxonomy.md` records as
    the shape that fires one measurement later.
    """
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
            # One extra round trip per hand resolution -- `PostgresTitle
            # Repository.get` is a `session.get` on the primary key, so it is
            # one indexed `SELECT` on a command an operator runs by hand, one
            # line at a time. Priced rather than assumed, the way
            # `attach_title`'s own comment prices its statement eleven lines
            # from here.
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
    (ADR-0043 §4). **A second `usher work` container is a second registry and
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

        async def _pass() -> int:
            """One pass, and in the daemon form a bug in it costs the pass
            rather than the process.

            **The arm `api/lanes.py`'s worker lane has had since M6, arriving
            at the other root of the same worker** (M10 F10). `JobWorker._pass`
            re-raises the first task failure after every task has settled, so
            without this one job's `AttributeError` ends `usher work` while the
            identical job under `USHER_WORKER_ENABLED=true` costs the lane one
            pass -- two survival semantics for one defect, in a deployment an
            operator picks between with a setting, and nothing said so.

            🔴 **`logger.exception`, never `logger.warning`, and that is the
            whole reason this arm is safe to have.** Issue #8 is a crash that
            left two lines and no frames; an arm that swallowed a bug and
            logged a *message* would turn a dead worker -- which is at least
            visible -- into a healthy-looking one that silently retries a
            deterministic fault. The stack is what makes the next occurrence
            evidence, and it is the deliverable here even though the cause is
            still unknown. `telemetry.configure_logging` sets `diagnose=False`,
            so the frames carry no locals and PRD 08's
            credentials-are-never-logged rule survives (`services/jobs.py`
            makes the same call for the same reason).

            ⚠️ **`--once` is deliberately outside the arm.** A cron entry and
            `docker compose exec usher python -m usher work --once` read the
            *exit code*, and a guard around this form would answer a crashed
            pass with `0` -- so the thing that exists to notice would be the
            last to. The daemon has no exit code to report with and its
            survival is the property; `--once` has no survival to protect and
            its exit code is. Pinned by
            `tests/unit/test_cli_work.py::
            test_one_pass_keeps_its_exit_code_rather_than_logging_and_returning`.

            **`Exception`, never `BaseException`:** `CancelledError` is how a
            SIGINT reaches this loop, and catching it would build a daemon that
            cannot be stopped out of the arm that stops it dying. Pinned by an
            **AST** case (`test_both_worker_roots_record_a_crashed_pass_with_
            its_frames`) rather than by a behavioural one, and that is
            `testing-discipline.md`'s rule about a failure mode that is a
            deadlock: a case that cancels this loop and waits can only report a
            timeout, and it does not even manage that -- the planted
            `BaseException` hangs the test runner's own teardown on the
            unstoppable task, so it produces no red line at all.

            Returning `0` is not a consolation value -- it is what makes the
            caller sleep `_IDLE_SLEEP_SECONDS` instead of hot-looping a
            failing pass, which is the same thing the lane's `ran = 0` does.
            The cost, named rather than discovered: a database outage now logs
            a stack per pass instead of a sentence per pass. The *rate* is
            unchanged, only the size, and the arm cannot tell an outage from a
            bug without re-litigating `OPERATOR_ERRORS` one layer down.
            """
            if once:
                return await _measure()
            try:
                return await _measure()
            except Exception as exc:
                logger.exception(
                    "the worker pass failed; the daemon continues: {error}", error=str(exc)
                )
                return 0

        ran = await _pass()
        print(f"{ran} jobs, {recovered} recovered claims")
        while not once:
            if ran == 0:
                await asyncio.sleep(_IDLE_SLEEP_SECONDS)
            taken = recovered
            ran = await _pass()
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


async def _schedule(settings: Settings, *, once: bool) -> None:
    """Run the scheduled-work loop, or one tick of it (ADR-0046).

    Mirrors `usher work` / `usher work --once`.

    🔴 **`--once` is one tick, and the period still gates it.** The decision is
    stated here because ADR-0046 does not state it and sells `--once` as the
    answer for a wall-clock schedule -- an operator's 3am cron -- without
    saying whether the period still applies. It does, for two reasons. A
    `--once` that ignored the period would make an operator's crontab entry an
    unconditional *"start the three-and-a-half-hour rebuild now"*, which is a
    different and much sharper command than *"tick"*; and it would give the
    same command two behaviours depending on a flag, so the daemon and the
    cron would disagree about what is due.

    ⚠️ **The consequence is a real limit on the sentence ADR-0046 writes, and
    it is not fixed here.** *"Every night at 3am"* is only what an operator
    gets if the job's period is comfortably under a day -- and a
    `ScheduledJob.period` is a **property of the job**, not a setting, so
    nothing an operator configures can lower it. A cron firing exactly one
    period apart is a coin flip on a boundary comparison, and one firing more
    often than the period silently no-ops on the ticks in between. The honest
    statement is that `--once` gives an operator control over *when the
    scheduler looks*, never over what it decides. `usher similar --rebuild` is
    still the command that runs a batch unconditionally.
    `tests/unit/test_cli_schedule.py::
    test_one_tick_does_not_run_a_job_whose_period_has_not_elapsed` is what
    pins it.

    ⚠️ **`USHER_SCHEDULER_ENABLED` is not read here**, and that is not an
    oversight. The setting gates the *lane*, i.e. whether the server process
    runs the loop unasked; this command is an operator running it on purpose,
    the way `usher work` runs regardless of `USHER_WORKER_ENABLED`. What the
    setting still owes an operator who uses this command is the reminder in
    ADR-0046's decision 3: nothing excludes a second runner, so a crontab
    entry beside a server with the lane on is two runners for one artefact.

    **Builds an engine and opens no connection**, which is
    `create_app`'s lifespan property and is deliberate here for the same
    reason: `build_scheduler` wires a session *factory* into the one
    registration that exists, and the first connection is opened by the first
    `last_done()` inside the first tick. A `--once` run against a database
    that is down therefore fails as a logged job failure and exit 0 rather
    than as a stack trace from a connection pool, which is what an operator's
    crontab wants at 3am. The engine is disposed however the command ends --
    ⚠️ **including the daemon form, which never ends normally**: `finally`
    runs on the `CancelledError` a SIGINT produces, and not on SIGKILL.
    """
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
            # was due" from "nothing is registered" -- and the second was the
            # shipped state for one commit, so a line that hid it would have
            # read as a healthy night on a deployment where the scheduler
            # could never do anything.
            print(f"{ran} of {registered} scheduled jobs ran")
            return
        print(f"scheduling {registered} jobs every {settings.scheduler_tick_seconds:g}s")
        await scheduler.run()
    finally:
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


async def _genres(
    settings: Settings,
    *,
    backfill: bool,
    batch_size: int,
    limit: int,
    after: uuid.UUID | None,
) -> None:
    """Report how much of `titles.genres` is written in a source's spelling,
    or rewrite it into Usher's own vocabulary.

    **Its own subcommand rather than a flag on `index` or `derive`, and the
    two rejections are the argument.** `usher index` is about
    `title_embeddings` — its `--backfill` enqueues jobs for a worker that owns
    a model, and folding a `titles` rewrite into it would make the command that
    reports search freshness also a writer of the catalog. `usher derive`
    re-derives people, credits, collections and artwork *from cached TMDb
    payloads*: it needs a `MetadataProvider` to exist and declines to run
    without one, reads `raw_payloads`, and writes four other tables. This
    reads no payload, needs no provider and no model, and writes one column.
    Three commands, three artefacts —
    [ADR-0026](../../docs/prd/decisions/0026-the-cli-boundary-names-families.md)'s
    family rule applied to what a command *is about* rather than to what it
    happens to be near.

    **The bare form only reads**, which is the bargain `index` and `derive`
    already take, so it is safe on a production box while diagnosing
    something. It is a full page-walk rather than a `count(*)` because the
    only definition of "this row needs rewriting" is `canonicalise_genres`,
    and a `WHERE` clause naming the alias spellings would be a second one
    living in SQL. See `TitleRepository.list_genres_page`.

    **`--after` is what makes an interrupt cheap rather than merely safe.**
    Re-running from the start is already correct — the map is idempotent and
    the write is guarded by `IS DISTINCT FROM`, so an already-normalised
    prefix costs one index probe per row and writes nothing — but on 1.27M
    rows that is a scan an operator need not repeat, so every run prints the
    cursor to resume from.
    """
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
    # **Expect this to be far smaller than the rewrite count, and that is the
    # finding rather than a defect**: the embedded population is the enriched
    # tier and the source spellings are almost entirely on skeletons. Measured
    # on the live catalog 2026-08-19, 79,913 rows move and 304 embeddings go
    # stale. A skeleton whose genre moved is not stale because it was never
    # embedded, not because the fingerprint missed it.
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
    #
    # ⚠️ **Its denominator is the *enriched* tier, not the catalog**, so
    # `1.000` says the backfill has drained and not that the vector lane can
    # see everything this search matched -- skeletons are never embedded and
    # the lexical lane searches them anyway. On the catalog this project
    # measures the two differ by an order of magnitude. `SearchOutcome` carries
    # the argument; the label is left as the field's own name because that is
    # what PRD 07 and the route call it, and a second name here would be a
    # second thing to keep in step.
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

    **A household, since M10's J2, resolved exactly as `usher search` resolves
    one** -- `ensure_default_user`, not `default_user`, because this command
    needs an id and nothing else and PRD 01's authentication seam is a
    singleton row until a request has one to carry. Nothing on this path reads
    it except the `search_queries` row, whose `user_id` is `NOT NULL` behind a
    real foreign key.

    **Not committed here, and that is what makes the row survive.**
    `_session_for` yields a session and disposes the engine without ever
    committing, so the household row and the analytics row are both durable
    only because `SearchService` commits them itself -- a suggest writer that
    inherited the caller's commit boundary would be correct on the route and
    silently lose every row this command wrote. The two rows are in one
    transaction, so a commit carrying the analytics row without its household
    would be refused rather than silently partial.
    """
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
    """The whole-table half of issue #17's *"staleness is at least
    observable"*: how old `title_neighbors` is, and how much of it was computed
    under a different blend.

    **Two facts, and they answer different questions** -- the port says so and
    this command is where an operator meets it. `computed_at()` is
    `min(computed_at)`, the **oldest** stored row, so it is an upper bound on
    the artefact's freshness and covers the half of staleness no per-row
    predicate can decide: a title's neighbours go stale when some *other* title
    is embedded. `stale_neighbors()` is exact and covers the other half, rows
    whose blend fingerprint is not the running one. **Neither subsumes the
    other, and a zero from the second is not a fresh table.**

    ⚠️ **Zero stale is also what an empty table reports**, which is why the age
    line comes first and says *never* rather than printing nothing: on a
    deployment where `m09e` emptied the table, "0 stale" and "3.3M rows, all
    current" are the same two characters.

    **What this deliberately does not print: how many embedded titles have no
    neighbour row at all.** That is a real number -- 922 of 133,364 on the
    catalog this project measures, 2026-09-07 -- and it is invisible to both
    reads here, because a missing row has no fingerprint to disagree and no
    timestamp to be old. It needs a count neither port offers, and inventing
    one here would put a third definition of "stale" in front of an operator.
    ADR-0046 records the same gap: a scheduler that is on does not make the
    artefact complete.
    """
    computed_at = await pipeline.similar.computed_at()
    if computed_at is None:
        print("no neighbours have ever been computed -- run `usher similar --rebuild`")
    else:
        age = datetime.now(UTC) - computed_at
        # Hours, because the walk is measured in hours and a period is too. A
        # day-resolution line cannot distinguish "finished an hour ago" from
        # "finished this morning", which is the comparison an operator makes.
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
    """Report the table's age, read one title's neighbours, or recompute it.

    **Three forms, and the argumentless one is M10's.** `usher similar` with
    nothing after it answers issue #17's *"staleness is at least observable --
    a count, a timestamp, or a `usher similar` line that says how old the table
    is relative to the embedding population"*. The per-title form below already
    printed two of those three facts and there was no whole-table spelling of
    any of them, so an operator asking *"does this table need rebuilding"* had
    to pick a title id at random and infer.

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
    fresh. ⚠️ **M10's J6 makes it *schedulable* and still not automatic** --
    `similar.rebuild` runs this same batch on a period, behind
    `USHER_SCHEDULER_ENABLED`, which is `false` by default. A deployment that
    has not opted in is exactly where M6 left it, which is why the paragraph
    above stands rather than being struck out.

    **`--resume` and `--max-seeds` are arguments to the walk and are refused
    without `--rebuild`**, accepted-and-ignored being the failure that matters:
    an operator who typed `--max-seeds 100` and got a report would believe
    they had capped a run that never started. The scheduled registration
    always resumes; an operator chooses.
    """
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        if rebuild:
            report = await pipeline.similar.rebuild(resume=resume, max_seeds=max_seeds)
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


async def _backup(settings: Settings, *, output: Path | None) -> None:
    """Write one artifact holding everything nothing else can rebuild.

    **The first command in this project whose ordinary failure is *"the disk
    is full"* or *"that directory does not exist"*, and it needs no new
    handler for either.** ADR-0026's Uncertainty section predicted a
    milestone that *"adds a subprocess, a message broker or a filesystem
    watcher adds a family with it"*; this is that milestone, the family is
    `OSError`, and `OPERATOR_ERRORS` has carried it since M7's smoke test --
    a refused TCP connection reaches asyncpg unwrapped, so it was already
    there for a completely different reason and covers this for free. So
    `usher backup` is inside the boundary the way M8's `usher curate` was:
    a `_dispatch` arm and a parser row, and nothing else. The prediction is
    noted in the ADR because a prediction that resolves silently is one
    nobody learns from.

    **It reads and never writes the database**, which makes it safe on a
    production box -- the bargain `usher index`, `usher derive` and bare
    `usher genres` already take.

    **The report says the `USHER_SECRET_KEY` thing on every run**, not behind
    a flag and not only when a credential row exists. `source_credentials`
    travels as ciphertext and this command holds no key, so an artifact
    restored into a deployment with a different key restores credentials
    nobody can decrypt; an operator who learns that at restore time learns it
    too late, and the run that most needs the sentence is the one against a
    deployment that has not added its source yet.
    """
    async with _session_for(settings) as session:
        service = BackupService(repository=PostgresBackupRepository(session))
        report = await service.write(output)
    _print_backup_report(report)


#: How many refused rows `_print_restore_report` names before it summarises.
#:
#: 🔴 **K5's drill printed a 14,176-line report**, restoring the real artifact
#: into an empty catalog: 14,166 refusals, one line each, plus the per-table
#: block. *"Every refusal is named and none is summarised away"* is right at 41
#: and unusable at 14,166 -- a five-figure wall of text is not a report, it is
#: the thing an operator scrolls past to reach the summary they needed.
#:
#: **Twenty because that is a screen.** It is more than the number of carried
#: tables (8), so a refusal in every table is still visible with room to see the
#: rungs repeat; and it is small enough that the summary line and the per-table
#: counts stay on the same screen as the detail, which is the whole point of
#: printing detail at all. The count is never truncated -- `refused_by_table()`
#: is exact whatever this is -- so what the cap costs is the *identity* of rows
#: 21..N, and `--dry-run` plus the artifact itself are where those live.
_REFUSALS_NAMED: Final = 20


async def _restore(
    settings: Settings, *, artifact: Path, dry_run: bool, skip_unresolvable: bool
) -> None:
    """Merge one artifact into this database, in one transaction, or refuse it.

    **Inside ADR-0026's boundary with no handler of its own**, exactly as
    `usher backup` is. `OSError` covers the artifact that is not there and the
    directory that is not readable; `DBAPIError` covers the database that is
    not up; both have been in `OPERATOR_ERRORS` since before this command
    existed. What is caught here is `RestoreRefused`, and that is not a
    boundary -- it is the shape ADR-0026 permits and `_curate` already uses
    twice: a command that knows what a failure *means* renders it. The
    alternative would be a tenth member of the tuple for a type only this
    command can raise.

    **The session is `_session_for`'s and there is exactly one**, which is
    what makes the one-transaction claim true rather than aspirational: the
    service commits once at the end and rolls back otherwise, and the engine
    is disposed however the command ends.

    **A run that refused exits non-zero**, `_sync`'s precedent and its
    argument: the refusals are already on stdout for a human, and cron, CI and
    a systemd unit read the exit code. `--dry-run` does **not** exit non-zero
    on its own -- an operator asking what would happen and being told got the
    answer they asked for -- but a dry run that found refusals does, because
    that is the same answer `usher restore` would have given.
    """
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

    🔴 **It used to say *"enrich or import what the lines above name"*, and for
    the one rung a correctly rebuilt catalog actually fails on that is false.**
    K2's ladder is `imdb_id`, then `(kind, tmdb_id)`, then the raw id -- and the
    third is a *check on the target*, not a key. A title reaches it only by
    carrying neither provider id, which on the deployment this project measures
    means an unmatched **stub** the ingest ladder created: it is in no IMDb
    dump, has no TMDb id to enrich by, and a rebuild mints it a new UUID. So
    the sentence sent an operator to run an importer that cannot possibly
    help. Measured 2026-08-25 -- 6 such titles of 1,272,891, accounting for
    **304 of the artifact's `media_items` rows**, which refused the whole file
    including 3,347 resolved watch states.

    The line now separates the two: a reference naming a provider id is
    something an importer fixes, and one naming only an id is not, and the
    second names the flag rather than an errand.
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
    """Five counts per table, then the refusals by name up to a cap, then one
    summary.

    **Five numbers rather than one**, which is the whole reason this command
    reports at all: *"restored 9 rows"* over an artifact holding 50 is the
    failure it exists to make visible, and an operator at a terminal has no
    second copy of the database to compare against.

    🔴 **`present` and `absent` were one column headed *"already present"*,
    and K5's drill printed it against a table holding zero rows.**
    `media_items 0 written / 10,515 already present` where
    `SELECT count(*) FROM media_items` answered **0** -- the rows were not
    present, there was nothing there to write onto, and the two are opposite
    instructions: *already linked* means the restore was unnecessary, *nothing
    here yet* means run `usher sync` and restore again. That second state is
    universal on the very recovery path this command exists for, because
    `media_items` rows come from a walk and the walk needs the `sources` row
    the artifact carries.

    **The refusal list is capped at `_REFUSALS_NAMED` with an exact tail.**
    The same drill printed **14,176 lines**. What a cap costs is the identity
    of rows 21..N; what it keeps is the per-table counts, which are computed
    from the whole list and are exact whatever is printed -- so *"how bad is
    it and where"* survives and *"which forty-first row"* moves to `--dry-run`
    and the artifact. The keys printed are `keys_tried`'s own rendering, so
    what an operator reads is what was looked for rather than a paraphrase.
    """
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
    """`--new-key` exists so that typing it is a refusal rather than a leak.

    🔴 **It was a silent success until 2026-08-26.** `argparse`'s
    `allow_abbrev` defaults to `True`, so `--new-key` was an unambiguous
    *prefix* of `--new-key-env` and bound to it: the operator's key arrived as
    `args.new_key_env`, i.e. as a variable *name*, and the "is not set"
    message printed it back twice -- once as `$<key>` and once inside a
    suggested `export <key>=...` an operator might paste. Every reason
    `--new-key-env` exists (shell history, `ps` output) was defeated by an
    abbreviation, and the case asserting the key is absent from the namespace
    could not see it because the namespace was exactly the right *shape* --
    the wrong value was in the right field.

    **Declared rather than merely disallowed**, because the three ways to
    refuse it are not equally good and this was measured over seven
    invocation shapes:

    - `allow_abbrev=False` alone stops the binding, but `--new-key K
      --new-key-env V` then reaches `parse_args`' *"unrecognized arguments:
      %s"*, **which prints the key**. Fixing the binding introduces a leak.
    - Declaring `--new-key` catches the exact spelling before any of that,
      with a message that names the flag and never the value.
    - `parse_args` refuses this command's unrecognised arguments without
      them, which closes what is left (`--newkey`, `--new-k`).

    All three ship. `help=argparse.SUPPRESS` keeps it out of `--help` and out
    of the usage line, so the surface still advertises exactly one way to
    name a key and this is a tripwire rather than an alternative.
    """

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
    """Read the new key out of the environment, and refuse it here or nowhere.

    **The value never appears in `argv`**, which is the whole reason
    `--new-key-env` names a *variable* rather than taking a key: a key on a
    command line is in the shell's history file and in `ps` output for every
    user on the box, and neither is undone by the command finishing. What
    `argparse` carries is the name, which is why `usher rotate-secret
    --new-key-env USHER_NEW_SECRET_KEY` is safe to paste into a runbook.

    **Validated by the same rules as the old key, before the engine is
    built.** `Settings.secret_key` is `Field(min_length=32)` with a
    `_reject_placeholder_secret_key` validator, and a rotation to a key
    `Settings` would refuse is a rotation that bricks the next start -- so the
    refusal has to arrive here rather than from pydantic at the next boot,
    with the credentials already re-encrypted under it. It is spelled as a
    real `Settings` construction rather than as a second copy of the two
    rules, for the reason `db/repositories/_errors.py` exists: two copies of a
    check are two chances to lose one, and a check that re-implements what it
    is checking against cannot fail the way the original does.

    `database_url` is passed through so the construction cannot fail for a
    reason that has nothing to do with the key; everything else re-reads the
    same environment `get_settings()` already validated.

    ⚠️ **Export the variable; do not put it in `.env`.** Measured 2026-08-25:
    `USHER_NEW_SECRET_KEY` exported into the environment is invisible to
    `Settings` (pydantic-settings' env source reads only the fields it
    declares), and the *same* name written into `.env` makes **every** entry
    point fail with `usher_new_secret_key: Extra inputs are not permitted` --
    `extra="forbid"`, the failure `config.COMPOSE_ONLY_PREFIX`'s comment
    records for `USHER_HOST_PORT`. Worse here than there: a pydantic
    `ValidationError` renders `input_value=`, so the leftover line leaks the
    new key into the traceback of anything that reads settings without a
    boundary. `settings_rejection` is what keeps this path's own refusal
    clean.

    ## 🔴 Two refusals before the environment is read, and the reason is that
    ## this argument is where an operator puts the key by mistake

    Everything above is true of a *correct* invocation. The defect it does not
    cover, found in review 2026-08-26: an operator who means "here is the new
    key" and types it here. `argparse` had already made that easy -- see
    `build_parser`'s `allow_abbrev` comment -- and this function then printed
    what it was given, twice, in a message and in a copy-pasteable `export`.

    - **A name that is not an environment variable name is refused, and the
      name is not printed.** POSIX spells a name `[A-Za-z_][A-Za-z0-9_]*`;
      `openssl rand -base64 32` contains `+/=` and a hyphenated key contains
      `-`, so the commonest way to reach this refusal is to have passed a key.
      Printing it back is the whole defect.
    - **A name `Settings` would accept as a `secret_key` is refused too, and
      this is the half a grammar check cannot see.** The documented way to make
      a key is `openssl rand -hex 32`, whose output is 64 lowercase hex
      characters -- a *legal* variable name whenever it starts with `a`-`f`,
      which is **6/16 = 37.5%** of the time (measured over 100,000 samples:
      37.6%). So better than a third of the time the operator's real key would
      have sailed through the grammar and been echoed by the "is not set"
      message below. The predicate is `Settings`' own acceptance rather than a
      length literal, for the reason the key check below is: `min_length=32` is
      the bound that decides, and restating it is a second copy to lose.

    **Only after both does the "is not set" message echo the name, and that is
    deliberate.** A well-formed name that cannot be a key is not a secret, and
    an operator who forgot the `export` needs to see which variable this
    command looked for.
    """
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
    """Re-encrypt every stored credential under a new `USHER_SECRET_KEY`.

    **Inside ADR-0026's boundary with no handler of its own**, exactly as
    `usher backup` and `usher restore` are: `DBAPIError` covers a database
    that is not up and `OSError` a connection that is refused, and both have
    been in `OPERATOR_ERRORS` since before this command existed. Nothing
    here catches a port error, because nothing here raises one --
    `CredentialCiphertextStore` does not decrypt, so a row no key opens is a
    `None` and a counted refusal rather than a `PortDataMalformed`. That is
    the per-command *handling* the ADR permits, and it is why this task does
    not add a tenth member to the tuple.

    **The key is read and validated before `_session_for` opens anything**,
    so a refused key is a rotation that touched no row -- asserted from a
    second session in `tests/integration/test_rotation.py`, because *"nothing
    was written"* against the writer's own session is satisfied by a service
    that never committed.

    **`build_cipher` twice, here, because this is the composition root.**
    `pyproject.toml`'s third import contract forbids `usher.services` naming
    `usher.db`, and the derivation lives in `db/repositories/credentials.py`
    beside the column it opens. So the service is handed two `Fernet` objects
    and never a `SecretStr`, which is also what makes the order of the two
    arguments a thing a test can be about: swapped, every row on the old key
    is refused and every row already on the new one is rotated *backwards*.

    **A run that refused exits non-zero**, `_sync`'s and `_restore`'s
    precedent: the refused refs are on stdout for a human, and cron, CI and a
    systemd unit read the exit code.
    """
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
    """Two diagnoses behind one counter, and only one of them is destructive.

    🔴 **Measured by M10's K8 drill, 2026-08-26.** With `USHER_SECRET_KEY`
    already set to the *new* key -- an operator who edited `.env` before
    running this, which is the likeliest mistake this command has -- `_rotate`
    builds `old_cipher` from that same key, both ciphers are one cipher, and
    every row still on the previous key opens under neither. Three seeded rows
    reported `rotated 0, already 0, refused 3` with **nothing written**: all
    three ciphertexts byte-identical afterwards and all three still opening
    under the old key. The same rows and the same command with only the order
    corrected reported `rotated 3`.

    The command cannot tell that state from three corrupt rows -- both arrive
    as `_plaintext` answering `None` twice -- so the *count* is what carries
    the distinction. **A saturated counter implies a different cause than its
    partial values**, and until this function existed the sentence written for
    the partial case was the one an operator acted on: *"must be re-entered --
    re-register those sources"*, said over credentials that were intact,
    unwritten and one environment variable away from rotating. Obeying it
    re-types every credential in the deployment to fix a problem that is not
    there.

    So the saturated arm names the key and **does not mention re-registration
    at all**. That omission is the fix rather than a tone change, and
    `tests/unit/test_cli_rotation.py` asserts the absence.

    ⚠️ **The predicate is `len(refused) == report.rows`, not `not
    report.rotated`.** A second run over a table an earlier run finished
    reports its rows as `already`, so the shorter spelling would call
    *"two already, one refused"* saturated and reassure an operator about a row
    that really is unreadable. The two are distinguishable only when `already`
    is non-empty, which is exactly the resumption path this command is built
    around.
    """
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
    # **`action="version"`, and the placement is load-bearing.** `main` parses
    # before it opens the error boundary, so argparse raises `SystemExit(0)`
    # here -- before `get_settings()` runs and before anything reaches a
    # database. That is what lets `docker run --rm <image> python -m usher
    # --version` answer on a host with no Postgres, and it is the property a
    # subcommand could not have: a subcommand is dispatched from inside the
    # `try`, after the settings are read.
    #
    # The number is interpolated from `usher.__version__` rather than written
    # here. It is `importlib.metadata.version("usher")`, which hatchling copies
    # from `[project].version` into the distribution's `METADATA` at install
    # time -- so a fourth place to edit is exactly what this avoids.
    # `tests/unit/test_release_metadata.py` guards the agreement, and records
    # that a red there means a `pyproject.toml` bumped without a `uv sync`.
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
        # The ordering warning is on the *option*, not only in the report,
        # because an operator scheduling `credit-names` reads `--help` before
        # they ever see a report -- and getting this order wrong is not
        # recoverable by re-running the phase. `PHASES` is `BootstrapPhase`'s
        # own order and that enum's docstring carries the measurement.
        #
        # **The help text says "a full run walks them in this order" and not
        # "the choices are in execution order", because two of the choices are
        # not steps.** `all` and `ratings` are aliases
        # (`domain.bootstrap.PHASE_ALIASES`), and `ratings` is declared beside
        # the phase whose second half it is -- so `--help` renders it second,
        # where the older sentence told an operator `--phase all` runs it
        # second. It runs it inside `imdb` and never dispatches it.
        #
        # `%%`, not `%`: argparse interpolates a help string against its own
        # parameter dict, so a bare `%` raises `TypeError` from `--help` and
        # from nothing else. Found by running it -- ruff, mypy and every
        # existing case pass against the broken spelling, because none of them
        # renders help.
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
    # for an operator's own crontab -- which is the supported path for a
    # wall-clock schedule a `ScheduledJob.period` cannot express (ADR-0046).
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
    # flight. A number to keep in mind, not a measured optimum.
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

    # The eighteenth subcommand. **The count is stated with the date it was
    # measured rather than maintained**, which is the habit ADR-0026's own
    # "fourteen the CLI advertised on 2026-08-05" bullet uses and the habit
    # this task's plan did not: written 2026-08-13 it said `backup` would be
    # the sixteenth, and `genres` and `eval` landed in between. On
    # **2026-08-25** `len(build_parser()._subparsers…choices)` is 18 with this
    # row, and `test_the_argv_table_covers_every_subcommand` compares
    # `_MINIMAL_ARGV` against `subparsers.choices` -- so nothing anywhere has
    # to hold the number.
    #
    # ⚠️ **The citation was a `grep` for one commit and the grep counted this
    # comment.** `grep -c "add_parser(" src/usher/cli.py` answered **19**,
    # because the line stating the claim contained the literal it searched
    # for -- writing the measurement down is what falsified it. Cite the
    # parser's own `choices`, which is the source the next sentence already
    # names and which cannot self-match; if a grep is wanted anyway, the
    # pattern has to be anchored (`sub\.add_parser(`) and then *that*
    # spelling has to stay out of the prose. Same family as the harness whose
    # landing-check was derived from the same guess as its edit.
    backup = sub.add_parser("backup", help="write everything nothing else can rebuild to one file")
    # `type=Path` rather than `str` plus a conversion in `_dispatch`: argparse
    # is where the surface is described, and a `--output` that is a string
    # here and a `Path` there is two spellings of one argument. No `default=`
    # -- the name embeds the run's own timestamp, so the default has to be
    # computed at the instant the header is stamped or the two disagree by
    # however long `Settings` and the engine took to build.
    backup.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write it; default usher-backup-<UTC>.jsonl.gz here",
    )

    # The nineteenth subcommand, stated with the date it was measured rather
    # than maintained -- the habit the row above records, and the correction
    # this one inherits. **The plan for this task said seventeenth**, which was
    # the count the parser advertised before `genres` and `backup` landed. On
    # **2026-08-25** `len(build_parser()._subparsers._group_actions[0].choices)`
    # is 19 with this row. Cite the parser's own `choices` and never
    # `grep -c "add_parser("`: that grep answers one too many, because the line
    # stating the claim contains the literal it searches for.
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
    # decision. *Allow* names a permission and reads as *let them through*,
    # which is the one thing this must not mean: the row is **dropped**, not
    # written with a null and not written with a guessed id. The flag's name
    # has to be the thing that happens to the row, because the operator reading
    # it in a runbook at 3am is deciding whether they can afford it.
    #
    # Opt-in with the refusal unconditional by default, which is the operator's
    # binding decision: *"refuses rather than half-applies"* is the command's
    # headline guarantee and it is not weakened silently.
    restore.add_argument(
        "--skip-unresolvable",
        action="store_true",
        help=(
            "drop rows naming a title or episode this catalog cannot resolve, "
            "instead of refusing the file; the next `usher sync` re-derives them"
        ),
    )

    # The twentieth subcommand, stated with the date it was measured rather
    # than maintained -- the habit the two rows above record, and the
    # correction they inherit. On **2026-08-26**
    # `len(build_parser()._subparsers._group_actions[0].choices)` is 20 with
    # this row, one day and two commands after the row above read 18. Cite
    # the parser's own `choices` and never
    # `grep -c "add_parser("`: that grep answers one too many, because the
    # line stating the claim contains the literal it searches for.
    #
    # 🔴 **`allow_abbrev=False`, and it is a security control rather than a
    # style.** It defaults to `True`, so before 2026-08-26 `--new-key` was an
    # unambiguous prefix of `--new-key-env` and argparse silently bound the
    # operator's key into the field meant for a variable *name*.
    #
    # **Set here and not on the top-level parser, because a subparser does not
    # inherit it.** Measured 2026-08-26 on a parser of this exact shape: with
    # `allow_abbrev=False` on the *outer* parser and nothing on the subparser,
    # `rotate-secret --new-key <key>` still binds.
    # `_SubParsersAction.add_parser` constructs a fresh `ArgumentParser` from
    # the keyword arguments it is handed and inherits nothing else, so a
    # subcommand's prefix matching is the subcommand's own.
    #
    # **Blast radius, stated because a reviewer asked for it before it landed:
    # this subparser's three option strings and nothing else.**
    # `--new-key-env`, `--new-key` and `--help`. Abbreviations of those stop
    # working *on this command only* -- `--new-key-e` was accepted yesterday
    # and is refused today; `-h` is an exact string and is unaffected. Every
    # other subcommand keeps prefix matching, so `usher derive --back` still
    # works. Nothing in `tests/`, `docs/` or `README.md` spells an abbreviated
    # flag for any command, so the wider setting was available; it is declined
    # because no other command has an argument whose *value* could be a
    # credential, and one measured defect is not evidence about nineteen
    # surfaces an operator may have muscle memory for.
    rotate = sub.add_parser(
        "rotate-secret",
        help="re-encrypt stored credentials under a new USHER_SECRET_KEY",
        allow_abbrev=False,
    )
    # The tripwire, before the real argument so a reader meets the refusal
    # first. `_RefuseAKeyOnTheCommandLine` carries the measurement.
    #
    # `dest=argparse.SUPPRESS` as well as `help=`: the action refuses before it
    # could ever store anything, so a `new_key` key on the namespace would be a
    # permanent `None` that exists only to be misread -- and it would weaken
    # `test_rotate_secret_takes_a_variable_name_and_never_a_key`, whose whole
    # content is that this command's namespace has exactly three keys and none
    # of them can hold a key.
    rotate.add_argument(
        "--new-key",
        nargs="?",
        action=_RefuseAKeyOnTheCommandLine,
        dest=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    # **A variable name, never a key**, and it is required rather than
    # defaulted. A key passed as `--new-key <value>` is in the shell's history
    # and in `ps` output; naming the variable keeps the value out of `argv`
    # entirely, and `test_rotate_secret_takes_a_variable_name_and_never_a_key`
    # asserts that by running this parser and greping the namespace.
    #
    # ⚠️ That case asserts a *shape* and the abbreviation defect satisfied it,
    # so the ones with teeth are the behavioural pair next to it: the key
    # passed as `--new-key` and the key passed to `--new-key-env` itself must
    # each be refused with the value absent from stdout, stderr **and** the
    # exit message together.
    #
    # No `default="USHER_NEW_SECRET_KEY"`: this command rewrites every stored
    # credential in the deployment, and a default would let a bare
    # `usher rotate-secret` pick up a variable left over from a previous run.
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
    """`parser.parse_args`, except that `rotate-secret`'s unrecognised
    arguments are refused **without** them.

    🔴 **argparse's own last step prints what it did not recognise**, and on
    one command in this CLI that is very likely a secret key. CPython's
    `ArgumentParser.parse_args` is exactly `parse_known_args` followed by
    `self.error(_('unrecognized arguments: %s') % ' '.join(argv))`, so
    `usher rotate-secret --new-key <key> --new-key-env VAR` answers
    `usher: error: unrecognized arguments: --new-key <key>` -- measured
    2026-08-26, on stderr, with the key in it. That is a leak `allow_abbrev=
    False` *introduces* rather than removes: with prefix matching on, the same
    argv bound the key silently instead.

    The body below is that same two-step, faithfully, with one branch: for
    `rotate-secret` the extras are counted and not shown. Every other command
    reaches the identical message argparse would have produced, because the
    refusal there is an ordinary typo and naming it is how an operator fixes
    it.

    **Keyed on the parsed `command` rather than on `argv[0]`**, because
    `--traceback` is a top-level flag and `usher --traceback rotate-secret …`
    is a legal spelling whose first token is not the subcommand.
    """
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
        # ⚠️ **No arguments is now the whole-table report and no longer an
        # error.** It was refused as "a read of nothing" until M10's J6, which
        # is what issue #17's *"a `usher similar` line that says how old the
        # table is relative to the embedding population"* asks for -- the
        # per-title form already printed two of the three facts and had no
        # whole-table spelling. A title id **with** `--rebuild` is still
        # refused: that is a read and a write in one command.
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
