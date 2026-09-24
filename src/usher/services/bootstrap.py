"""The resumable, checkpointed bulk-import loop (PRD 04, Phases 0-2)."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset
from usher.ports.errors import (
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    UsherPortError,
)
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.repository import (
    BulkCatalogRepository,
    GenomeCoverage,
    GenomeRepository,
    ImportRunRepository,
)

_tracer = trace.get_tracer("usher.bootstrap")
_meter = metrics.get_meter("usher.bootstrap")

# PRD 10's metric catalogue. Created at import time against whatever
# MeterProvider `configure_metrics` installed -- a real SDK provider
# unconditionally, exported only when an OTLP endpoint is set.
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


#: The members of the taxonomy that say "ask again later": the upstream could not be
#: reached or answered in time, or asked to be backed off. Everything else a dataset
#: raises -- above all `PortDataMalformed` -- is the same answer on every attempt.
TRANSIENT: tuple[type[UsherPortError], ...] = (PortUnavailable, PortRateLimited)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long `import_dataset` keeps resuming a dataset whose upstream failed transiently.

    **Bounded per unit of work, not per phase.** A *streak* is the run of failures at
    one checkpoint; a committed batch ends it, so a long walk with scattered failures
    completes while one unit that never answers gives up. A streak ends after
    `attempts` attempts (the first included) or once the next wait would carry it
    past `budget` seconds from its first failure, whichever comes first.

    The waits double from `first_delay` up to `max_delay`, and a `Retry-After` hint is
    a floor under them, never a ceiling. No jitter: one process retrying one upstream
    has nobody to fall into step with.

    The shipped numbers are WDQS's: its query limit is 60 s and its timeouts arrive
    after ~65 s, so five attempts with waits of 15, 30, 60 and 120 s spend at most
    ~10 minutes on one page before the phase fails -- resumably, where it stopped.
    """

    attempts: int = 5
    first_delay: float = 15.0
    max_delay: float = 120.0
    budget: float = 900.0

    def delay(self, retry: int, exc: UsherPortError) -> float:
        """Seconds to wait before retry number `retry` (1-based)."""
        backoff = min(self.first_delay * 2.0 ** (retry - 1), self.max_delay)
        hint = exc.retry_after if isinstance(exc, PortRateLimited) else None
        return backoff if hint is None else max(backoff, hint)


#: Read when a service is built rather than bound as a default, so a case can replace
#: it for everything a composition root constructs.
DEFAULT_RETRY = RetryPolicy()


class _GaveUp(Exception):
    """A transient failure that outlasted its `RetryPolicy`, with the count that proves it.

    Private and not an `UsherPortError`: it never leaves `import_dataset`, which records
    it exactly as it records the port error it wraps -- with how hard it tried appended,
    because "failed" and "failed five times over four minutes" ask different things of
    an operator.
    """

    def __init__(self, cause: UsherPortError, attempts: int, elapsed: float, reason: str) -> None:
        tried = f"{attempts} attempt{'' if attempts == 1 else 's'} over {elapsed:.0f}s"
        super().__init__(f"{cause} (gave up after {tried}{reason})")
        self.cause = cause


class _FetchFailed(Exception):
    """A transient port error raised by the *dataset*, as opposed to by the writer.

    Only the fetch is retried: a writer that raised may have left half a batch in the
    transaction, and resuming over it is a decision about the writer's atomicity this
    loop cannot make.
    """

    def __init__(self, cause: UsherPortError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _warn(line: str) -> None:
    """Where a retry notice goes when nobody handed the service a sink."""
    logger.warning("{line}", line=line)


class VocabularyState(StrEnum):
    """Whether the stored tag vocabulary can name the lanes of the stored vectors.

    Five members and not four: "there is nothing to name" and "there is
    something to name and no names" are different operator actions, and
    collapsing them is how a fresh database ends up being told to re-run a
    phase it has no use for. `MIXED_RELEASES` is not a verdict about the
    vocabulary at all -- with `genome_scores` holding two releases there is no
    single revision to ask for, and asking for either would report the
    vocabulary as wrong when what is wrong is the vectors.

    A member rather than a string because both surfaces branch on it: the CLI
    renders a sentence and the route puts the member on the wire, so a client
    can distinguish the five without parsing English.
    """

    #: `genome_scores` is empty, so there are no lanes to name.
    NO_VECTORS = "no_vectors"
    #: `genome_scores` holds more than one release; not judged.
    MIXED_RELEASES = "mixed_releases"
    #: Vectors exist and `genome_tags` is empty; the fix is `--phase movielens`.
    NOT_LOADED = "not_loaded"
    #: A vocabulary is stored and it was loaded from another release.
    MISMATCHED = "mismatched"
    #: The vocabulary names the lanes; `tags` carries how many.
    NAMED = "named"


@dataclass(frozen=True, slots=True)
class VocabularyVerdict:
    """`VocabularyState` plus whatever that state has to carry.

    Two optional fields rather than five subclasses: exactly two states carry
    anything. `MISMATCHED` carries the port's own message, which names both
    release tokens -- the port's diagnosis, not a surface's prose.
    """

    state: VocabularyState
    tags: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """Everything `bootstrap-status` describes, assembled once.

    Two aggregate reads on every call, priced for an operator screen rather
    than a client one: `count_titles()` is a seq-scan `count(*)`, and three of
    `genome_coverage()`'s five terms are themselves full scans of `titles`. So
    a report costs hundreds of milliseconds and grows with the catalog rather
    than with what is on the screen. Fine behind an admin page somebody opens
    on purpose; do not reuse this shape on a client path.

    Deliberately not cached: it would have to be invalidated by a writer in
    another process, on the one page an operator opens precisely because they
    do not trust what they last saw.
    """

    runs: tuple[ImportRun, ...]
    titles: int
    genome: GenomeCoverage
    vocabulary: VocabularyVerdict


async def vocabulary_verdict(
    genome: GenomeRepository, coverage: GenomeCoverage
) -> VocabularyVerdict:
    """`GenomeRepository.vocabulary`'s operator surface, as a decision.

    The one function both surfaces call. It lives here rather than in
    `usher.cli` because a route that re-derived it would be a second answer to
    *"what does 'not loaded' mean?"*, and the branch the two would disagree on
    is the one nobody ever looks at.

    The refusal is caught and turned into a state, not raised:
    `PortDataMalformed` is deliberately not in `cli.OPERATOR_ERRORS`, being a
    content error rather than a transport one, so letting it out would answer
    "what state is my genome in?" with a stack trace at a terminal and a 500
    on the wire.

    Takes the port rather than a session, which is the seam that keeps the
    five branches unit-testable.
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
    """Everything `usher bootstrap-status` reads, as one value.

    Assembled here rather than inside either surface: the CLI prints it and
    `GET /admin/bootstrap/status` serialises it, and a report built twice is
    two answers waiting to drift. It takes ports and not a session, so both
    roots hand it whatever they already hold.

    No read here can fail on an empty database, which is PRD 08's operator
    rule and the reason this returns a report for every state rather than
    raising for some.
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


def _cursor(run: ImportRun, revision: str) -> BulkCursor | None:
    """The cursor a run resumes from, or `None` for one at the very start."""
    if not run.position:
        return None
    return BulkCursor(revision=revision, position=run.position, rows_seen=run.rows_seen)


async def _fetched[RowT](
    batches: AsyncIterator[BulkBatch[RowT]],
) -> AsyncIterator[BulkBatch[RowT]]:
    """`batches`, with a transient failure *of the fetch* marked as one.

    The marking is what lets `_drain_resuming` retry the dataset and never the writer,
    whose own failures pass through `_drain`'s body untouched.
    """
    iterator = aiter(batches)
    while True:
        try:
            batch = await anext(iterator)
        except StopAsyncIteration:
            return
        except TRANSIENT as exc:
            raise _FetchFailed(exc) from exc
        yield batch


class BootstrapService:
    """Drives one `BulkDataset` into the catalog, resumably."""

    def __init__(
        self,
        runs: ImportRunRepository,
        catalog: BulkCatalogRepository,
        commit: Callable[[], Awaitable[None]],
        *,
        events: EventPublisher,
        phase: BootstrapPhase,
        report: Callable[[str], None] | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`report` takes one line per retry; the log gets it as a WARNING when absent.

        One channel, not both: under the CLI the log is stdout too, so a sink *and*
        a log line would print every retry twice.
        """
        self._runs = runs
        self._catalog = catalog
        self._commit = commit
        self._events = events
        self._phase = phase
        self._report = report or _warn
        self._retry = retry or DEFAULT_RETRY
        self._sleep = sleep
        self._clock = clock

    async def import_dataset[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        *,
        revision: str | None = None,
    ) -> ImportRun:
        """Stream `dataset` through `write`, checkpointing every batch."""
        started = time.perf_counter()
        with _tracer.start_as_current_span("bootstrap.import") as span:
            span.set_attribute("usher.dataset", dataset.name)
            try:
                # The caller's already-resolved value, so this run and the batches it
                # streams cannot straddle two revisions.
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
                    resume_from = _cursor(run, resolved)
                    if resume_from is not None:
                        logger.info(
                            "resuming {dataset} from position {position} "
                            "({rows} rows already seen)",
                            dataset=dataset.name,
                            position=resume_from.position,
                            rows=resume_from.rows_seen,
                        )
                    run = await self._drain_resuming(dataset, write, run, resolved)
            except (UsherPortError, _GaveUp) as exc:
                # Case 2. `_GaveUp` is recorded as the port error it wraps, with the
                # attempts appended to the message.
                cause = exc if isinstance(exc, UsherPortError) else exc.cause
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
                _failures.add(1, {"dataset": dataset.name, "kind": type(cause).__name__})
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
        """Case 1 of `import_dataset`.

        `self._runs.start()` lost the race to create `dataset`'s row to a concurrent
        process.
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

    async def _drain_resuming[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        run: ImportRun,
        revision: str,
    ) -> ImportRun:
        """`_drain`, resumed from the last committed checkpoint after a transient failure.

        **A retry is a resume**: it re-reads the checkpoint the failed attempt left and
        hands the dataset that cursor -- the same thing an operator re-running the
        phase gets, done without them. Nothing already committed is fetched or written
        again. Bounded by `RetryPolicy`; a streak that outlasts it raises `_GaveUp`,
        which `import_dataset` records as `FAILED` at the checkpoint reached.
        """
        attempt = 1
        streak_started: float | None = None
        while True:
            try:
                return await self._drain(dataset, write, run, _cursor(run, revision), revision)
            except _FetchFailed as failed:
                exc = failed.cause
                committed = (await self._runs.get(dataset.name)) or run
                now = self._clock()
                if streak_started is None or committed.position != run.position:
                    # A batch committed since the last failure, or this is the first:
                    # either way this failure opens a new streak.
                    attempt, streak_started = 1, now
                run = committed
                elapsed = now - streak_started
                if attempt >= self._retry.attempts:
                    raise _GaveUp(exc, attempt, elapsed, "") from exc
                wait = self._retry.delay(attempt, exc)
                if elapsed + wait > self._retry.budget:
                    raise _GaveUp(
                        exc,
                        attempt,
                        elapsed,
                        f": the next wait would pass the {self._retry.budget:.0f}s retry budget",
                    ) from exc
                self._report(
                    f"{dataset.name}: attempt {attempt} of {self._retry.attempts} failed "
                    f"at position {run.position}: {exc}; retrying in {wait:.0f}s"
                )
                await self._sleep(wait)
                attempt += 1

    async def _drain[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        run: ImportRun,
        resume_from: BulkCursor | None,
        revision: str,
    ) -> ImportRun:
        async for batch in _fetched(dataset.batches(resume_from=resume_from, revision=revision)):
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
                # The single commit that makes this resumable: rows and cursor land
                # together or not at all.
                await self._commit()
                # After that commit, never before it: an event is a statement about
                # committed state, and this frame's subject is the batch the line
                # above just made durable.
                await self._publish_progress(run)
            _rows_counter.add(written, {"dataset": dataset.name})
            _batch_duration.record(time.perf_counter() - batch_started, {"dataset": dataset.name})
        return await self._finish(run)

    async def _publish_progress(self, run: ImportRun) -> None:
        """One `bootstrap.progress` per committed batch, scoped to no title.

        Per batch rather than per run: an admin UI's progress bar is the whole
        point of the event, and one at the end is a bar that jumps from 0% to
        100%. That is why the `bootstrap` job's registration hands this service
        the process bus rather than `JobWorker`'s deferred buffer.

        Scoped to no title, which is what makes PRD 07's *"Admin UI only"* true
        rather than advisory: a bulk import touching most of the catalog would
        otherwise wake every detail screen in the household once per batch.

        No `percent`: a byte offset cannot honestly supply one.
        """
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.BOOTSTRAP_PROGRESS,
                data={
                    "dataset": run.dataset,
                    "phase": self._phase.value,
                    "rows_seen": run.rows_seen,
                    "rows_written": run.rows_written,
                    "position": run.position,
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
