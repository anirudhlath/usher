"""PRD 03's reconciliation lanes: the nightly full walk and the delta walk.

**The availability sweep is on the success path, and nowhere else.**
`SourceAdapter.list_items` is contracted to raise rather than truncate
precisely so a caller can tell "the library ended" from "the adapter gave
up"; `usher/adapters/emby/adapter.py` calls a lost item "the one failure
this port exists to make impossible". That guarantee is worth nothing unless
the thing that acts on it declines to act when the walk raised, so:

- a walk that raises records the run `FAILED` with its error and reaches no
  sweep at all;
- a walk that completes reaches the sweep, which may still refuse
  (ADR-0015) if it would retract more of the source than the configured
  ceiling;
- **only a `FULL` walk sweeps.** A delta walk returns only what changed, so
  by construction nearly everything is "unseen"; sweeping after one would
  retract the library.

**The guard is not a substitute for any of those three.** It fires at a
*fraction*, so it rescues a catastrophe and misses a quiet one: a walk that
failed after writing eight of ten items leaves two stale rows, and 20% is
under the ceiling. Moving the sweep into a `finally:` therefore retracts two
perfectly healthy items and reports a successful-looking failure --
`tests/unit/test_services_reconcile.py` is built around exactly that
arithmetic, because the obvious version of the case passes under the
mutation.

Batches are committed as they go, with the run's counters, for the same
reason `BootstrapService` commits a batch and its cursor together: 1,126,674
items is hours, and a crash must cost the batch in flight rather than the
walk. Unlike bootstrap there is no mid-walk cursor to resume from *on these
two lanes* -- the port offers `since` and nothing finer for `list_items` --
so a crashed full walk is re-run from the start, which is safe because every
write is an upsert and the sweep never ran.

**The watch lane is the exception since ADR-0042**, and the difference is a
fact about the two lanes' *cursors* rather than a disagreement about design.
`SourceAdapter.watch_state` grew a `start_index` and
`WatchStateSyncService` checkpoints it on `sync_runs.position`, because
restarting costs the two lanes different things. An item lane resumes from
the newest completed walk of *either* item kind, and the measured deployment
has 13 completed delta runs, so a restart here costs a delta window. The
watch lane had never completed a single run, so its `since` was `None` and
every restart was the whole library -- ~1.14M items, about eleven hours --
and it never once converged (#41). Nothing on this lane wants that
machinery; its cursor advances.

A refused sweep fails the *run* and keeps the run's writes. The mirror-image
bug is real and worse: if a refusal discarded the batches the walk committed,
a source that has genuinely shrunk past the ceiling could never record
anything again, because the upsert half of every subsequent walk would be
rolled back along with the refusal.

`commit` is injected rather than a session being passed in: `services/` may
depend only on `domain/` and `ports/` (PRD 01, layering rule 2), and a
session is neither. Same shape `BootstrapService` already uses.
"""

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

# The two lanes that walk `list_items`. A delta resumes from whichever of them
# last completed: they differ only in whether a `since` was passed, so a full
# walk that finished at 03:00 is a perfectly good floor for a delta at noon --
# and reading only `DELTA` would re-walk a window the nightly run already
# covered. `WATCH_STATE` is deliberately absent: it walks a different method
# under a different upstream filter (`MinDateLastSavedForUser`, measured as
# genuinely different) and owns its own cursor.
_ITEM_LANES: tuple[SyncRunKind, ...] = (SyncRunKind.FULL, SyncRunKind.DELTA)


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

    async def reconcile(self, source: Source, kind: SyncRunKind, adapter: SourceAdapter) -> SyncRun:
        """Walk `source` and reconcile it. Never raises a `UsherPortError`.

        Like `BootstrapService.import_dataset`, a failed run leaves a
        durable, inspectable record rather than a traceback: an operator
        running `usher sync` across three sources needs the second and third
        to run when the first is unreachable. Anything that is *not* a
        `UsherPortError` propagates untouched -- a bug here is not an
        upstream failure and must not be recorded as one.
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
                await self._walk(source, progress, adapter, cursor)
                # Reached only when the walk returned normally. The whole
                # safety argument is one `try` boundary wide.
                run = await self._sweep(progress.run, kind)
                run = run.evolve(status=SyncRunStatus.COMPLETED, finished_at=datetime.now(UTC))
            except UsherPortError as exc:
                # `progress.run`, never the pre-walk `run`: the batches this
                # walk already committed are real, and recording the failure
                # over a stale copy would erase their checkpoint.
                run = progress.run.evolve(
                    status=SyncRunStatus.FAILED,
                    # str(exc), never the exception object and never a
                    # payload -- PRD 08's credentials-never-logged rule, and
                    # `error` is a Text column an operator reads.
                    error=str(exc),
                    finished_at=datetime.now(UTC),
                )
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

    async def cursor_for(self, source: Source, kind: SyncRunKind) -> AwareDatetime | None:
        """`None` for a full walk; the newest completed item-lane run's start
        instant for a delta.

        **Public because `None` is the answer to "how big is this walk", and a
        caller has to be able to ask before committing to one.**
        `LaneSupervisor._close_gap` asks exactly this before it decides whether
        a reconnect delta is a bounded window or the entire library
        (`USHER_PUSH_GAP_CLOSE`). Reusing this method rather than reading
        `latest_completed_cursor` at the call site is what keeps the answer the
        lane logs and the answer the walk uses from drifting: the "later of
        both item lanes" rule below is stated once.

        A full walk must ignore every cursor: one that inherited a `since`
        would return only what changed and then sweep, which is the exact
        combination ADR-0015 exists to make unreachable.

        A delta reads *both* item lanes and takes the later. Only completed
        runs count -- resuming from a run that failed halfway skips
        everything it never reached, and does it silently -- which is why
        `latest_completed_cursor` is the method rather than "the newest run".
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
    ) -> None:
        batch: list[SourceItem] = []
        async for item in adapter.list_items(since=cursor):
            batch.append(item)
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(source, progress.run, batch)
                batch = []
        if batch:
            # The trailing partial batch. A walk's item count is almost never
            # a multiple of the batch size, so omitting this drops the last
            # page of nearly every walk -- and the sweep then retracts exactly
            # those items on the next run.
            progress.run = await self._flush(source, progress.run, batch)

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

    async def _sweep(self, run: SyncRun, kind: SyncRunKind) -> SyncRun:
        """Retract availability -- full walks only, and only after one
        finished."""
        if kind is not SyncRunKind.FULL:
            return run
        try:
            result = await self._media_items.mark_unseen_unavailable(
                run.source_id,
                seen_since=run.started_at,
                max_retract_fraction=self._max_retract_fraction,
            )
        except AvailabilitySweepRefused as exc:
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
        return run.evolve(items_retracted=result.retracted)
