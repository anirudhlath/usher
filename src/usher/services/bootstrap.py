"""The resumable, checkpointed bulk-import loop (PRD 04, Phases 0-2).

One loop, shared by every dataset. Its whole job is the invariant that makes
"resumable" true: **a batch's rows and the cursor that describes them are
committed in the same transaction.** Commit the rows first and a crash claims
work it never did; commit the cursor first and a crash silently loses rows.

Instrumentation lives here rather than being deferred to M10, per the spec's
"instrumentation is cross-cutting, not a milestone": one span per run, one per
batch, plus the four metrics PRD 10's catalogue gained for this milestone.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.bootstrap import (
    DATASET_PHASES,
    BootstrapPhase,
    ImportRun,
    ImportRunStatus,
)
from usher.ports.bulk import BulkCursor, BulkDataset
from usher.ports.errors import PortDataMalformed, RepositoryConflict, UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.repository import (
    BulkCatalogRepository,
    GenomeCoverage,
    GenomeRepository,
    ImportRunRepository,
)

_tracer = trace.get_tracer("usher.bootstrap")
_meter = metrics.get_meter("usher.bootstrap")

# PRD 10's metric catalogue, M2's four. Created at import time against
# whatever MeterProvider `configure_metrics` installed -- which is a real SDK
# provider unconditionally, exported only when an OTLP endpoint is set.
_rows_counter = _meter.create_counter(
    "usher.bootstrap.rows", unit="1", description="Rows written by a bulk importer"
)
_batch_duration = _meter.create_histogram(
    "usher.bootstrap.batch.duration", unit="s", description="Wall time per committed batch"
)
_phase_duration = _meter.create_histogram(
    "usher.bootstrap.phase.duration", unit="s", description="Wall time per dataset import"
)
_failures = _meter.create_counter(
    "usher.bootstrap.failures", unit="1", description="Bulk imports that ended in failure"
)


class VocabularyState(StrEnum):
    """Whether the stored tag vocabulary can name the lanes of the stored
    vectors — the **decision**, with the sentence left to whoever renders it.

    Five members and not four: "there is nothing to name" and "there is
    something to name and no names" are different operator actions, and
    collapsing them is how a fresh database ends up being told to re-run a
    phase it has no use for. `MIXED_RELEASES` is the one that is *not* a
    verdict about the vocabulary at all — with `genome_scores` holding two
    releases there is no single revision to ask for, and asking for either
    would report the vocabulary as wrong when what is wrong is the vectors.

    A member rather than a string because both surfaces branch on it:
    `usher bootstrap-status` renders a sentence and
    `GET /admin/bootstrap/status` puts the member on the wire, so a client
    can distinguish the five without parsing English.
    """

    #: `genome_scores` is empty, so there are no lanes to name.
    NO_VECTORS = "no_vectors"
    #: `genome_scores` holds more than one release; not judged.
    MIXED_RELEASES = "mixed_releases"
    #: Vectors exist and `genome_tags` is empty — every catalog bootstrapped
    #: before `m08b` is in this state, and the fix is `--phase movielens`.
    NOT_LOADED = "not_loaded"
    #: A vocabulary is stored and it was loaded from another release.
    MISMATCHED = "mismatched"
    #: The vocabulary names the lanes; `tags` carries how many.
    NAMED = "named"


@dataclass(frozen=True, slots=True)
class VocabularyVerdict:
    """`VocabularyState` plus whatever that state has to carry.

    Two optional fields rather than five subclasses, because exactly two
    states carry anything: `NAMED` carries a count and `MISMATCHED` carries
    the port's own message, which names **both** release tokens. That message
    is the port's diagnosis rather than a surface's prose — the same string
    `ImportRun.error` stores — so passing it through is not the "route
    serialising English" this report exists to avoid.
    """

    state: VocabularyState
    tags: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """Everything `bootstrap-status` describes, assembled once.

    ⚠️ **Two aggregate reads on every call, and they are priced for an
    operator screen rather than a client one.** Measured 2026-08-12 against a
    real 1,272,367-title catalog with a 15,565-vector genome (`\\timing`,
    median of five, on a *busy* box, so these are upper bounds):
    `count_titles()` is a seq-scan `count(*)` at **80.6 ms**;
    `genome_coverage()` is **248.6 ms** for its five-way aggregate plus
    **2.0 ms** for the revisions read, because three of its five terms are
    themselves full scans of `titles`. So one report is roughly **a third of
    a second**, and it grows with the catalog rather than with what is on the
    screen. Fine behind an admin page somebody opens on purpose; **do not
    reuse this shape on a client path.**

    Deliberately **not cached.** A cache would be an unmeasured mechanism on
    the one page an operator opens precisely because they do not trust what
    they last saw, and it would have to be invalidated by a writer in another
    process. The number is stated instead, which is what makes the cost a
    decision rather than a surprise.
    """

    runs: tuple[ImportRun, ...]
    titles: int
    genome: GenomeCoverage
    vocabulary: VocabularyVerdict


async def vocabulary_verdict(
    genome: GenomeRepository, coverage: GenomeCoverage
) -> VocabularyVerdict:
    """`GenomeRepository.vocabulary`'s operator surface, as a decision.

    **The one function both surfaces call.** It lives here rather than in
    `usher.cli` because a route that re-derived it would be a second answer to
    *"what does 'not loaded' mean?"*, and the branch the two would disagree on
    is the one nobody ever looks at.

    The refusal is *caught and turned into a state*, not raised:
    `PortDataMalformed` is deliberately not in `cli.OPERATOR_ERRORS` — the
    three `UsherPortError` subclasses ADR-0026's amendment added are the
    transport ones and this is a content one — so letting it out would answer
    "what state is my genome in?" with a stack trace about the answer being
    bad, at a terminal and with a 500 on the wire.

    Takes the port rather than a session, which is the seam that keeps the
    five branches unit-testable: `cli._status` opens its own engine and is
    not.
    """
    if not coverage.revisions:
        return VocabularyVerdict(state=VocabularyState.NO_VECTORS)
    if len(coverage.revisions) > 1:
        return VocabularyVerdict(state=VocabularyState.MIXED_RELEASES)
    try:
        names = await genome.vocabulary(coverage.revisions[0][0])
    except PortDataMalformed as exc:
        return VocabularyVerdict(state=VocabularyState.MISMATCHED, detail=str(exc))
    if names is None:
        return VocabularyVerdict(state=VocabularyState.NOT_LOADED)
    return VocabularyVerdict(state=VocabularyState.NAMED, tags=len(names))


async def bootstrap_report(
    runs: ImportRunRepository,
    catalog: BulkCatalogRepository,
    genome: GenomeRepository,
) -> BootstrapReport:
    """The four reads `usher bootstrap-status` has made since M2, as a value.

    Assembled here rather than inside either surface, for the reason
    `composition.run_bootstrap` is one dispatch two roots call: the CLI prints
    it and `GET /admin/bootstrap/status` serialises it, and a report built
    twice is two answers waiting to drift. It takes ports and not a session,
    so both roots hand it whatever they already hold.

    **No read here can fail on an empty database**, which is PRD 08's operator
    rule and the reason this returns a report for every state rather than
    raising for some: `list_runs()` answers `[]`, both aggregates answer zero,
    and `vocabulary_verdict` answers `NO_VECTORS` without asking the port
    anything at all.
    """
    stored = await runs.list_runs()
    titles = await catalog.count_titles()
    coverage = await catalog.genome_coverage()
    return BootstrapReport(
        runs=tuple(stored),
        titles=titles,
        genome=coverage,
        vocabulary=await vocabulary_verdict(genome, coverage),
    )


class BootstrapService:
    """Drives one `BulkDataset` into the catalog, resumably.

    `commit` is injected rather than a session being passed in: `services/`
    may depend only on `domain/` and `ports/` (PRD 01, layering rule 2), and a
    session is neither. The caller -- `usher.cli`, the composition root --
    supplies a zero-argument coroutine that commits its own unit of work.

    `events` and `phase` are **keyword-only and never defaulted**, on
    `ReconcileService`'s stated grounds (`services/reconcile.py:121`): a
    shared `NullEventPublisher()` in a signature is a mutable-looking default
    that is stateless only by accident, and every other collaborator here is
    required. The two composition roots supply one where they mean it --
    `composition.build_worker` the process bus, `cli._bootstrap` a real
    `NullEventPublisher` for a process that has no SSE client on the other
    side of a publish.

    **`phase` is the `BootstrapPhase` this run was asked for, not the dataset
    currently streaming**, and the frame carries both. `dataset` is the
    fine-grained identifier and moves through seven values on a `--phase all`
    run; `phase` is what an operator typed, what
    `POST /admin/bootstrap/{phase}` answered with, and what `Job.key` holds --
    so it is the field a client uses to tell *its* request's frames from
    somebody else's. The alternative was threading the phase through every
    `import_dataset` call in `composition`, which is the same fact spelled
    once per dataset.
    """

    def __init__(
        self,
        runs: ImportRunRepository,
        catalog: BulkCatalogRepository,
        commit: Callable[[], Awaitable[None]],
        *,
        events: EventPublisher,
        phase: BootstrapPhase,
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._commit = commit
        self._events = events
        self._phase = phase

    async def import_dataset[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        *,
        revision: str | None = None,
    ) -> ImportRun:
        """Stream `dataset` through `write`, checkpointing every batch.

        Returns the final `ImportRun` and never raises a `UsherPortError` --
        a failed phase must leave a durable, inspectable record and let the
        caller decide whether to continue with the next phase, which is what
        `bootstrap --phase all` needs to be useful when one upstream is down.
        Three distinct outcomes share that "does not raise" property, but
        only two of them are safe to persist:

        1. **`self._runs.start()` raises `RepositoryConflict`.** We lost a
           race to claim `dataset`'s row to a concurrent process and hold no
           `ImportRun` of our own -- `start()` never returned one. The only
           row that exists for `dataset` belongs to whichever process's
           insert actually won, quite possibly still `RUNNING`. Delegated to
           `_concede_to_other_owner`, which touches nothing: no `save`, no
           `commit`. Returns the winner's row exactly as stored, read-only,
           so a caller sees a real report instead of a crash but the durable
           record itself is left alone. This case does not fall through to
           case 2 below -- see the nested `try` in the implementation.
        2. **`revision()` raises, or `start()` succeeds and `_drain` raises
           afterward.** Either way we own (or have never yet touched) the
           run at `dataset.name`, so the except handler re-fetches whatever
           `self._runs` currently holds for it -- never this call's own
           `run` variable -- and records `FAILED` there. `_drain` checkpoints
           and commits after every batch it completes, using its *own* local
           `run` binding, so when it raises instead of returning, this
           method's `run` is still whatever `self._runs.start()` returned
           *before* any of that batch progress, stale by however many
           batches `_drain` already committed; evolving that stale value
           would silently regress the checkpoint backwards on every failure.
           Re-fetching by name is safe here specifically because we know we
           own the row: `ImportRunRepository.save`'s only conflict path is a
           fresh insert colliding with a concurrent fresh insert (case 1),
           and once any row exists for a dataset, every later `start()`/
           `save()` call updates that same row rather than competing for a
           new one -- so a `RepositoryConflict` cannot reach this branch
           under `_drain`'s normal write pattern (it only ever updates the
           id `start()` already gave us). `revision()` itself can raise
           either `PortUnavailable` or `PortRateLimited` (both real -- the
           shared download helper every M2 adapter's `revision()` delegates
           to maps a 429 straight to the latter), and both are
           `UsherPortError` subclasses, so catching the base class here
           catches both without needing to name either.
        3. **Anything that is not a `UsherPortError`** (from `start()`,
           `_drain`, or `write` itself) propagates untouched -- a bug in
           this process is not an upstream failure and must not be recorded
           as one.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("bootstrap.import") as span:
            span.set_attribute("usher.dataset", dataset.name)
            try:
                # `revision`, when given, is the value the caller already
                # resolved this run -- the same parameter `BulkDataset.batches`
                # carries and for a stronger reason than saving a HEAD.
                #
                # The `movielens` phase has to stamp `genome_scores
                # .genome_revision` with the release each row came from, so
                # its writer needs the value *during* the drain, and `write`
                # is handed rows and nothing else. Resolving it separately in
                # the caller and letting this method resolve it again leaves
                # two `HEAD`s that can disagree: upstream re-uploading between
                # them would download and stream release B while stamping
                # every row release A. That is a mislabelled row, which is the
                # precise failure `genome_revision` exists to make visible --
                # so the caller passes its value in and the two agree by
                # construction rather than by luck.
                #
                # Defaulted to `None`, so the four M2 call sites are
                # unaffected and still resolve it here.
                resolved = revision if revision is not None else await dataset.revision()
                span.set_attribute("usher.revision", resolved)
                try:
                    run = await self._runs.start(dataset.name, resolved)
                except RepositoryConflict as exc:
                    # Case 1 -- handled entirely inside this nested try, so
                    # it never reaches the outer `except UsherPortError`
                    # below and never triggers the re-fetch-and-overwrite
                    # that branch performs for case 2.
                    run = await self._concede_to_other_owner(dataset.name, resolved, exc, span)
                else:
                    # **The opening commit, and it is not bookkeeping.**
                    # `start()` flushes and does not commit (the port says so
                    # in as many words), so until some later `_commit()`
                    # carries it, the `RUNNING` checkpoint is invisible to
                    # every other connection -- including the one answering
                    # `GET /admin/bootstrap/status`. `_drain` commits after
                    # its first batch, which for `wikidata.crosswalk` is a
                    # SPARQL round trip away and for a dataset resuming at
                    # head never arrives at all. So an operator who pressed
                    # Run was told nothing was running, and was told it
                    # truthfully: from outside this transaction, nothing was.
                    # One commit per dataset -- eight for a full run, against
                    # the thousands `_drain` already pays -- buys a row that
                    # is readable the moment it exists.
                    await self._commit()
                    await self._publish_progress(run)
                    resume_from = (
                        BulkCursor(
                            revision=resolved, position=run.position, rows_seen=run.rows_seen
                        )
                        if run.position
                        else None
                    )
                    if resume_from is not None:
                        logger.info(
                            "resuming {dataset} from position {position} "
                            "({rows} rows already seen)",
                            dataset=dataset.name,
                            position=resume_from.position,
                            rows=resume_from.rows_seen,
                        )
                    run = await self._drain(dataset, write, run, resume_from, resolved)
            except UsherPortError as exc:
                # Case 2. self._runs.get(), not this call's own `run`
                # binding -- see the docstring above for why that binding
                # can be either nonexistent (revision() failed first) or
                # stale (_drain committed progress under its own local `run`
                # before raising), and why re-fetching by name is safe here
                # specifically (we own this row, unlike case 1). Falls back
                # to a freshly-constructed run only for a dataset that has
                # never once gotten past revision() -- "unknown" satisfies
                # ImportRun.revision's min_length=1 and the matching DB
                # CHECK constraint, and start() overwrites it the moment
                # revision() next succeeds.
                run = (await self._runs.get(dataset.name)) or ImportRun(
                    dataset=dataset.name, revision="unknown"
                )
                run = run.evolve(
                    status=ImportRunStatus.FAILED,
                    # str(exc), never the exception object and never a
                    # payload: PRD 08's credentials-never-logged rule, and
                    # `error` is a Text column an operator reads.
                    error=str(exc),
                    heartbeat_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                await self._runs.save(run)
                await self._commit()
                # After the commit, exactly as the batch frames are: a
                # `FAILED` run is a normal, designed state that a screen
                # relabels "Resume", and it is the transition a client is
                # least able to infer from silence.
                await self._publish_progress(run)
                _failures.add(1, {"dataset": dataset.name, "kind": type(exc).__name__})
                span.set_attribute("usher.failed", True)
                logger.error(
                    "{dataset} import failed at position {position}: {error}",
                    dataset=dataset.name,
                    position=run.position,
                    error=str(exc),
                )
            finally:
                _phase_duration.record(time.perf_counter() - started, {"dataset": dataset.name})
        return run

    async def _concede_to_other_owner(
        self, dataset: str, revision: str, exc: RepositoryConflict, span: trace.Span
    ) -> ImportRun:
        """Case 1 of `import_dataset`: `self._runs.start()` lost the race to
        create `dataset`'s row to a concurrent process.

        `RepositoryConflict` only ever reaches `import_dataset` from
        `start()` itself -- see the surrounding method's docstring for why
        `_drain`'s own `save()` calls cannot trigger it under normal
        operation. That means whenever this method runs, we hold no
        `ImportRun` of our own to evolve: the one row that exists for
        `dataset` belongs entirely to whichever process's insert actually
        won, quite possibly still `RUNNING`, quite possibly already
        `COMPLETED`. Re-fetching it and saving a `FAILED` status on top --
        which is exactly what `import_dataset`'s case-2 handler does, and
        did here too before this method existed -- would silently overwrite
        that other process's real progress with a failure message
        describing *this* process's redundant attempt, not its own. That is
        worse than the crash it replaced: the crash was a loud, visible
        `PendingRollbackError`; a corrupted checkpoint is silent, and a
        subsequent resume reads exactly this record.

        So this method calls neither `self._runs.save()` nor `self._commit()`
        -- it only reads. Returns the current owner's record exactly as
        stored, for the caller to inspect or log. The synthetic fallback (a
        freshly-built, never-persisted `ImportRun`) exists only for the
        pathological case where the very row that caused this conflict is
        already gone by the time we look here -- deleted out from under both
        processes -- so a caller always gets *something* inspectable back
        without this method fabricating a claim over a dataset it just lost
        the race for.
        """
        owner = await self._runs.get(dataset)
        span.set_attribute("usher.conflict", True)
        logger.warning(
            "not recording a failure for {dataset}: {error} -- a different "
            "run already owns its checkpoint, leaving it untouched",
            dataset=dataset,
            error=str(exc),
        )
        return owner or ImportRun(
            dataset=dataset, revision=revision, status=ImportRunStatus.FAILED, error=str(exc)
        )

    async def _drain[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        run: ImportRun,
        resume_from: BulkCursor | None,
        revision: str,
    ) -> ImportRun:
        # `revision=revision`: the value `import_dataset` already resolved,
        # not left for `batches()` to re-derive. Cheap for an implementation
        # whose own `revision()` is already cheap (IMDb, Wikidata), but not
        # merely an optimisation for TMDb's daily export -- its own module
        # docstring notes an unresolved `revision` forces the multi-day
        # backward-scanning `_newest_available` walk all over again, and a
        # fresh re-resolve could in principle disagree with the value this
        # run already committed to across a UTC-midnight race.
        async for batch in dataset.batches(resume_from=resume_from, revision=revision):
            batch_started = time.perf_counter()
            with _tracer.start_as_current_span("bootstrap.batch") as span:
                span.set_attribute("usher.dataset", dataset.name)
                span.set_attribute("usher.batch.rows", len(batch.rows))
                written = await write(batch.rows)
                run = run.evolve(
                    revision=batch.cursor.revision,
                    position=batch.cursor.position,
                    rows_seen=batch.cursor.rows_seen,
                    rows_written=run.rows_written + written,
                    heartbeat_at=datetime.now(UTC),
                )
                await self._runs.save(run)
                # The single commit that makes this resumable: rows and cursor
                # land together or not at all. Fires even for a row-less
                # batch (an implementation may yield one solely to advance
                # the cursor past filtered-out records, per BulkDataset.
                # batches' contract) -- skipping the commit there would make
                # a resume replay the filtered-out run on every restart.
                await self._commit()
                # **After that commit, never before it.** ADR-0033: an event
                # is a statement about committed state, and this frame's
                # subject is the batch the line above just made durable --
                # `rows_seen`, `rows_written` and `position` are all read off
                # the `ImportRun` that is now in `import_runs`. A client
                # acting on the frame reads exactly that or newer. The
                # ordering is asserted rather than assumed: a fake records
                # how many commits had happened when each frame arrived.
                await self._publish_progress(run)
            _rows_counter.add(written, {"dataset": dataset.name})
            _batch_duration.record(time.perf_counter() - batch_started, {"dataset": dataset.name})
        return await self._finish(run)

    async def _publish_progress(self, run: ImportRun) -> None:
        """One `bootstrap.progress` per committed batch **and one per
        transition**, scoped to no title.

        **Per batch rather than per run**, because an admin UI's progress bar
        is the whole point of the event and one at the end is a bar that jumps
        from 0% to 100% -- the failure `ReconcileService._publish_progress`
        already names for `sync.progress`, and the reason the `bootstrap`
        job's registration hands this service the process bus rather than
        `JobWorker`'s deferred buffer (`composition.build_worker` carries that
        argument in full).

        ⚠️ **Per batch was *only* per batch until this, and that made the
        event undrivable.** `import_dataset` has three transitions a client
        cares about -- the run starting, finishing, and failing -- and none of
        them is a batch, so none of them raised a frame. The consequences were
        both real and both invisible from inside this module: a screen driven
        by frames showed a card the moment the first batch committed and then
        had no way to ever clear it, and a dataset that resumed at head
        committed no batch at all, so it ran to completion having said
        nothing. `_finish` and the failure arm now publish, and so does the
        opening -- see `import_dataset` for why that one also needed a commit
        it did not have.

        **The payload is the whole run, not a cursor**, which is what lets a
        client render from the frame instead of answering it with a
        request. `GET /admin/bootstrap/status` costs ~0.33 s and is uncached,
        so a frame that only says "something moved" buys a refetch per batch
        -- strictly worse than the poll it was meant to replace. Every field
        here is `ImportRunResponse`'s, spelled identically, so a client
        patching a status document with a frame never has to translate.

        **`phase` is the step that owns `dataset`; `requested_phase` is what
        was asked for.** They differ on every `--phase all` run, and only the
        first is the six-member vocabulary a console has a row for. Read from
        `DATASET_PHASES` rather than from `self._phase`, which is `all` for
        exactly the run where the distinction matters -- and `None`, never a
        fallback, for a dataset the map does not hold.

        **Scoped to no title**, which is what makes PRD 07's *"Admin UI only"*
        true rather than advisory: a `?titles=` subscriber never sees one, and
        a bulk import touching most of the catalog would otherwise wake every
        detail screen in the household once per batch.

        **No `percent`**, and the payload is what a cursor can honestly
        supply: `ClientEventKind.BOOTSTRAP_PROGRESS` carries the argument, and
        PRD 07's payload column is corrected rather than satisfied by a
        fraction invented from a byte offset.
        """
        owner = DATASET_PHASES.get(run.dataset)
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.BOOTSTRAP_PROGRESS,
                data={
                    "dataset": run.dataset,
                    "phase": owner.value if owner is not None else None,
                    "requested_phase": self._phase.value,
                    "status": run.status.value,
                    "revision": run.revision,
                    "position": run.position,
                    "rows_seen": run.rows_seen,
                    "rows_written": run.rows_written,
                    "error": run.error,
                    "started_at": run.started_at.isoformat(),
                    "heartbeat_at": run.heartbeat_at.isoformat(),
                    "finished_at": (
                        run.finished_at.isoformat() if run.finished_at is not None else None
                    ),
                },
            )
        )

    async def _finish(self, run: ImportRun) -> ImportRun:
        now = datetime.now(UTC)
        run = run.evolve(
            status=ImportRunStatus.COMPLETED, error=None, heartbeat_at=now, finished_at=now
        )
        await self._runs.save(run)
        await self._commit()
        # The frame that closes the run. Without it the last thing a client
        # hears about a dataset is a batch, which is indistinguishable from a
        # run that stalled -- and `heartbeat_at` older than 120 s is what a
        # screen turns into "Stalled?", so the silence does not merely fail to
        # inform, it eventually misinforms.
        await self._publish_progress(run)
        logger.info(
            "{dataset} import complete: {seen} rows seen, {written} written",
            dataset=run.dataset,
            seen=run.rows_seen,
            written=run.rows_written,
        )
        return run

    async def link_crosswalk(self) -> None:
        """Phase 2's final step: stamp stored pairs onto catalog titles.

        Separate from `import_dataset` because it consumes no dataset -- it is
        a single set-based statement over two tables Usher already holds, and
        it is idempotent, so re-running it after a partial crosswalk import is
        both safe and useful.
        """
        with _tracer.start_as_current_span("bootstrap.link_crosswalk") as span:
            result = await self._catalog.link_crosswalk()
            await self._commit()
            span.set_attribute("usher.linked", result.linked)
            span.set_attribute("usher.unmatched", result.unmatched)
            span.set_attribute("usher.conflicted", result.conflicted)
            logger.info(
                "crosswalk linked {linked} titles ({unmatched} not in catalog, "
                "{conflicted} blocked by an existing claim)",
                linked=result.linked,
                unmatched=result.unmatched,
                conflicted=result.conflicted,
            )
