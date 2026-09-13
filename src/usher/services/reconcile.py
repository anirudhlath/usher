"""PRD 03's reconciliation lanes: the nightly full walk and the delta walk."""

import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import AvailabilitySweepRefused
from usher.ports.repository import MediaItemRepository, SyncRunRepository
from usher.ports.source import SourceAdapter, SourceItem
from usher.services.ingest import IngestService

_tracer = trace.get_tracer("usher.reconcile")
_meter = metrics.get_meter("usher.reconcile")
_run_duration = _meter.create_histogram(
    "usher.sync.run.duration", unit="s", description="Wall time per sync run"
)

# : What fraction of a source a finished full walk retracted, or would have.
_retraction_fraction = _meter.create_histogram(
    "usher.sync.retraction.fraction",
    unit="1",
    description="Fraction of a source's items a full walk retracted, or would have",
)


def _fraction(part: int, whole: int) -> float:
    """`part / whole`, with an empty source recording a real 0.0.

    The guard itself is a **count comparison rather than a division** for this
    reason (`db/repositories/media_item.py:482-485`) -- an empty source divides
    by zero -- and a metric that skipped the record instead would reintroduce
    the silence the instrument exists to remove: a source with no items would
    publish nothing, which reads exactly like a source that was never swept.
    """
    return part / whole if whole else 0.0


# The two lanes that walk `list_items`.
_ITEM_LANES: tuple[SyncRunKind, ...] = (SyncRunKind.FULL, SyncRunKind.DELTA)

# The first token of a bounded walk's `error`, and the reason it is a constant rather
# than prose.
CEILING_ERROR_CODE = "gap_delta_ceiling"

# The same device for the *other* failure an operator has a command for, and added for
# the same reason one token over (M10 S9).
RETRACTION_ERROR_CODE = "availability_ceiling"


def _recorded_failure(exc: UsherPortError) -> tuple[str, str | None]:
    """The sentence and the kind `sync_runs` holds for a failure this service absorbed.

    Returned together so the two cannot be written apart: a `str(exc)` with no
    code beside it is a refusal the CLI stops offering its flag for, and the
    only reader that can tell which failure this is is the `isinstance` here.
    `None` is the honest answer for every other port error -- there is no
    command to offer, and a catch-all member is how one starts being offered
    for all of them.
    """
    if isinstance(exc, AvailabilitySweepRefused):
        return str(exc), RETRACTION_ERROR_CODE
    return str(exc), None


class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose, and it exists to fix a real defect rather than to
    read nicely. `SyncRun` is frozen and `_flush` saves an *evolved* copy
    after every batch, so a `_walk` that returns its final run leaves
    `reconcile`'s own binding at whatever it was **before any of that
    progress** the moment the walk raises. Evolving that stale value into the
    `FAILED` row then writes `items_seen = 0` over a checkpoint that had
    recorded eight -- the durable record lies about how far the run got, and
    PRD 10's dashboard 3 plots exactly that number.

    `BootstrapService.import_dataset` documents the identical trap one
    milestone down ("evolving that stale value would silently regress the
    checkpoint backwards on every failure") and solves it by re-fetching;
    there is no equivalent read here, because `SyncRunRepository` is a
    history rather than a per-source checkpoint and "the run I started" is
    only knowable by holding on to it.
    """

    __slots__ = ("run",)

    def __init__(self, run: SyncRun) -> None:
        self.run = run


class ReconcileService:
    def __init__(
        self,
        ingest: IngestService,
        media_items: MediaItemRepository,
        runs: SyncRunRepository,
        events: EventPublisher,
        commit: Callable[[], Awaitable[None]],
        *,
        batch_size: int = 1_000,
        max_retract_fraction: float = 0.25,
    ) -> None:
        self._ingest = ingest
        self._media_items = media_items
        self._runs = runs
        # Required, never a default: a shared `NullEventPublisher()` in a
        # signature is a mutable-looking default that is stateless only by
        # accident, and every other collaborator here is required. The two
        # composition roots supply one where they mean it.
        self._events = events
        self._commit = commit
        self._batch_size = batch_size
        self._max_retract_fraction = max_retract_fraction

    async def reconcile(
        self,
        source: Source,
        kind: SyncRunKind,
        adapter: SourceAdapter,
        *,
        max_items: int = 0,
    ) -> SyncRun:
        """Walk `source` and reconcile it.

        Never raises a `UsherPortError`.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.reconcile") as span:
            span.set_attribute("usher.source", source.name)
            span.set_attribute("usher.sync.kind", kind.value)
            cursor = await self.cursor_for(source, kind)
            run = SyncRun(source_id=source.id, kind=kind, cursor_at=cursor)
            # Inserted and committed before the walk begins, `RUNNING`: an
            # operator watching a six-hour sync needs a row to watch, and a
            # process killed mid-walk must leave a trace rather than nothing.
            await self._runs.add(run)
            await self._commit()
            progress = _Progress(run)
            try:
                truncated = await self._walk(source, progress, adapter, cursor, max_items)
                if truncated:
                    # Deliberately **not** `usher.failed`: the run is recorded `FAILED`
                    # because that is what stops the cursor, and a trace view that could
                    # not tell "the source broke" from "Usher stopped on purpose" would
                    # send an operator looking for an outage that did not happen.
                    span.set_attribute("usher.sync.truncated", True)
                    run = self._failed(
                        progress.run,
                        self._ceiling_error(progress.run),
                        code=CEILING_ERROR_CODE,
                    )
                    logger.warning(
                        "{kind} sync of {source} stopped after {seen} items, its "
                        "USHER_PUSH_GAP_MAX_ITEMS ceiling. The run is recorded FAILED so it "
                        "advances no cursor and the next delta re-requests what it never "
                        "reached; run `usher sync --kind full` for it to close the rest",
                        kind=kind.value,
                        # The source's **name**, never its base URL and
                        # never anything from its credential row -- PRD 08's
                        # credentials-are-never-logged rule, and the failure
                        # line below is the local precedent.
                        source=source.name,
                        seen=run.items_seen,
                    )
                else:
                    # Reached only when the walk returned normally *and*
                    # returned everything. The whole safety argument is one
                    # `try` boundary wide, and the ceiling is inside it: a
                    # bounded walk has items it never looked at, so a sweep
                    # after one would retract every one of them.
                    run = await self._sweep(progress.run, kind, source.name)
                    run = run.evolve(status=SyncRunStatus.COMPLETED, finished_at=datetime.now(UTC))
            except UsherPortError as exc:
                # `progress.run`, never the pre-walk `run`: the batches this walk
                # already committed are real, and recording the failure over a stale
                # copy would erase their checkpoint.
                error, code = _recorded_failure(exc)
                run = self._failed(progress.run, error, code=code)
                span.set_attribute("usher.failed", True)
                logger.error(
                    "{kind} sync of {source} failed after {seen} items: {error}",
                    kind=kind.value,
                    source=source.name,
                    seen=run.items_seen,
                    error=str(exc),
                )
            await self._runs.save(run)
            await self._commit()
            span.set_attribute("usher.items_seen", run.items_seen)
            span.set_attribute("usher.items_retracted", run.items_retracted)
        _run_duration.record(
            time.perf_counter() - started,
            {"source": source.name, "kind": kind.value, "status": run.status.value},
        )
        return run

    @staticmethod
    def _failed(run: SyncRun, error: str, *, code: str | None) -> SyncRun:
        """The one spelling of a terminal failure row.

        One function rather than the two identical `evolve` calls the two
        branches above would otherwise carry: a rule written twice is a rule
        one deletion is invisible in, and both branches depend on exactly the
        same thing being true -- `status` is `FAILED`, so
        `latest_completed_cursor` skips this run and the next walk of this
        lane resumes from wherever it resumed from.
        """
        return run.evolve(
            status=SyncRunStatus.FAILED,
            error=error,
            error_code=code,
            finished_at=datetime.now(UTC),
        )

    @staticmethod
    def _ceiling_error(run: SyncRun) -> str:
        """What `sync_runs.error` holds after a bounded walk.

        The sentence only. `CEILING_ERROR_CODE` goes in `error_code` beside
        it: the column is what a machine reads, this is what an operator
        reads, and neither is recoverable from the other.

        **It takes no `max_items`, and that is the honest shape rather than
        an omission.** The count and the ceiling are the same number by
        construction -- a truncated walk saw exactly `max_items` and stopped
        -- so a signature carrying both would invite a reader to render two
        numbers that can never differ. It is rendered once, and the ceiling
        is named by the setting an operator would change.
        """
        return (
            f"stopped after {run.items_seen} items, this walk's USHER_PUSH_GAP_MAX_ITEMS "
            f"ceiling. Nothing seen was lost and no cursor moved; run "
            f"`usher sync --kind full` for this source to close the rest"
        )

    async def cursor_for(self, source: Source, kind: SyncRunKind) -> AwareDatetime | None:
        """`None` for a full walk.

        the newest completed item-lane run's start instant for a delta.
        """
        if kind is not SyncRunKind.DELTA:
            return None
        cursors = [
            cursor
            for lane in _ITEM_LANES
            if (cursor := await self._runs.latest_completed_cursor(source.id, lane)) is not None
        ]
        return max(cursors) if cursors else None

    async def _walk(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        max_items: int,
    ) -> bool:
        """Walk the source into the catalog.

        `True` when it stopped at `max_items` with the source still holding more.
        """
        batch: list[SourceItem] = []
        pulled = 0
        truncated = False
        async for item in adapter.list_items(since=cursor):
            pulled += 1
            # `>`, not `>=`, and the difference is a whole cursor.
            if max_items and pulled > max_items:
                truncated = True
                break
            batch.append(item)
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(source, progress.run, batch)
                batch = []
        if batch:
            # The trailing partial batch, on both exits.
            progress.run = await self._flush(source, progress.run, batch)
        return truncated

    async def _flush(self, source: Source, run: SyncRun, batch: Sequence[SourceItem]) -> SyncRun:
        # `run.started_at`, not `now()`: `last_seen_at` means "the run that
        # saw this item", which is the quantity the sweep's
        # `last_seen_at < started_at` comparison is about. A per-row write
        # instant is a different quantity that happens to compare the same
        # way, and nothing downstream can recover the first from it.
        result = await self._ingest.ingest_batch(run.source_id, batch, observed_at=run.started_at)
        run = run.evolve(
            items_seen=run.items_seen + len(batch),
            items_matched=run.items_matched + result.matched,
            items_unmatched=run.items_unmatched + result.unmatched,
        )
        await self._runs.save(run)
        # One commit per batch, exactly like BootstrapService: a crash costs
        # the batch in flight, never the walk.
        await self._commit()
        await self._publish_progress(source, run)
        return run

    async def _publish_progress(self, source: Source, run: SyncRun) -> None:
        """One `sync.progress` per batch, scoped to no title.

        **Per batch rather than per run**, because an admin UI's progress bar
        is the whole point of the event and one at the end is a bar that
        jumps from 0% to 100%. A nightly walk of the one measured library
        flushes 1,127 of these.

        **Scoped to no title**, which is what makes PRD 07's "Admin UI only"
        true rather than advisory: a `?titles=` subscriber never sees one, and
        a detail screen that re-rendered on each of those 1,127 is the failure
        the filter exists for.

        The *name*, not the id: a payload a client renders should carry the
        name an operator configured, and `run.source_id` is a UUID nothing
        outside this process has a use for. That is why `Source` is threaded
        down here rather than re-read from a repository.
        """
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.SYNC_PROGRESS,
                data={
                    "source": source.name,
                    "kind": run.kind.value,
                    "items_seen": run.items_seen,
                    "items_matched": run.items_matched,
                    "items_unmatched": run.items_unmatched,
                },
            )
        )

    async def _sweep(self, run: SyncRun, kind: SyncRunKind, source_name: str) -> SyncRun:
        """Retract availability -- full walks only, and only after one finished.

        `source_name` is carried in for the metric's label rather than read off
        the run, which holds only a `source_id`: `usher.sync.run.duration`
        beside it is already labelled by name, and a second per-source identity
        in telemetry is what ADR-0043 §2 refuses.
        """
        if kind is not SyncRunKind.FULL:
            return run
        try:
            result = await self._media_items.mark_unseen_unavailable(
                run.source_id,
                seen_since=run.started_at,
                max_retract_fraction=self._max_retract_fraction,
            )
        except AvailabilitySweepRefused as exc:
            # The refusal's own numerator, never `SweepResult.retracted` --
            # which is what a refused sweep did, i.e. nothing. See the
            # instrument's own comment: the two differ exactly when the guard
            # fires, which is the one state this series exists for.
            _retraction_fraction.record(
                _fraction(exc.would_retract, exc.total),
                {"source": source_name, "outcome": "refused"},
            )
            logger.error(
                "availability sweep refused for source {source_id}: {error}",
                source_id=run.source_id,
                error=str(exc),
            )
            # Re-raised so `reconcile`'s own handler records it as a failed
            # run -- a refusal is not a successful reconcile with a footnote.
            # `AvailabilitySweepRefused` is a `UsherPortError`, so it lands
            # in the same branch a transport failure does.
            raise
        _retraction_fraction.record(
            _fraction(result.retracted, result.total),
            {"source": source_name, "outcome": "swept"},
        )
        return run.evolve(items_retracted=result.retracted)
