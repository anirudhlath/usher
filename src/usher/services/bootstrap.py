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
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.bulk import BulkCursor, BulkDataset
from usher.ports.errors import RepositoryConflict, UsherPortError
from usher.ports.repository import BulkCatalogRepository, ImportRunRepository

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


class BootstrapService:
    """Drives one `BulkDataset` into the catalog, resumably.

    `commit` is injected rather than a session being passed in: `services/`
    may depend only on `domain/` and `ports/` (PRD 01, layering rule 2), and a
    session is neither. The caller -- `usher.cli`, the composition root --
    supplies a zero-argument coroutine that commits its own unit of work.
    """

    def __init__(
        self,
        runs: ImportRunRepository,
        catalog: BulkCatalogRepository,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._commit = commit

    async def import_dataset[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
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
                revision = await dataset.revision()
                span.set_attribute("usher.revision", revision)
                try:
                    run = await self._runs.start(dataset.name, revision)
                except RepositoryConflict as exc:
                    # Case 1 -- handled entirely inside this nested try, so
                    # it never reaches the outer `except UsherPortError`
                    # below and never triggers the re-fetch-and-overwrite
                    # that branch performs for case 2.
                    run = await self._concede_to_other_owner(dataset.name, revision, exc, span)
                else:
                    resume_from = (
                        BulkCursor(
                            revision=revision, position=run.position, rows_seen=run.rows_seen
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
                    run = await self._drain(dataset, write, run, resume_from, revision)
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
            _rows_counter.add(written, {"dataset": dataset.name})
            _batch_duration.record(time.perf_counter() - batch_started, {"dataset": dataset.name})
        return await self._finish(run)

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
