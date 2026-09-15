"""Inbound watch state (PRD 03), and the backfill it leaves behind."""

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import UsherPortError
from usher.ports.ingest import MediaItemTarget, WatchStateMerge
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import MediaItemRepository, SyncRunRepository, WatchStateRepository
from usher.ports.source import SourceAdapter, SourceWatchState
from usher.telemetry import current_traceparent

_tracer = trace.get_tracer("usher.watch_sync")
_meter = metrics.get_meter("usher.watch_sync")
_run_duration = _meter.create_histogram(
    "usher.watch_state.run.duration", unit="s", description="Wall time per watch-state run"
)
_backfilled = _meter.create_counter(
    "usher.watch_state.backfilled", unit="1", description="Play histories recovered by a backfill"
)


@dataclass(frozen=True, slots=True)
class MergedState:
    """One state a merge was built for, and where it landed.

    The pair travels together because separating it is a real bug. The push
    lane publishes one client event per merged state, carrying that state's
    position and played flag against that state's target. Recovering the pairing
    outside this service -- by zipping the targets against the batch the caller
    handed in -- mis-pairs the moment the batch contains one unmatched item,
    because the targets are only the matched subset and `zip` aligns by
    position. It then publishes item A's resume position under item B's title
    id, which a client renders.

    Keyed, never aligned by position -- the rule `SourceEvent` states for
    `watch_states` one layer up.
    """

    external_id: str
    target: MediaItemTarget


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What one batch of inbound watch state did.

    `merged` is what a merge was *built* for, not the rows the repository
    changed -- the two differ whenever PRD 03's "latest `updated_at` wins"
    refuses one, and `SyncRun.items_matched` has always meant the first.
    Returning the repository's count in its place would silently change what
    every existing `sync_runs` row means and what PRD 10's dashboard plots.

    `rows_written` is the second, and is what the push lane publishes on:
    telling a client its watch state changed when nothing did is a
    re-render per echo of a position it set itself.

    `needing_history` is the ids already enqueued for the `WATCH_HISTORY`
    backfill, reported rather than re-derived -- a caller that recomputed
    `played and play_count is None` would be a second copy of the predicate, and
    two copies is how they come to disagree.
    """

    merged: tuple[MergedState, ...]
    unmatched: int
    rows_written: int
    needing_history: tuple[str, ...]


class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose, and for the reason `ReconcileService._Progress`
    states at length: `SyncRun` is frozen, `_flush` saves an evolved copy
    per batch, and a failure handler holding the pre-walk value regresses
    the durable checkpoint to zero on every failure.

    That checkpoint carries `position` too, and regressing *that* is not merely
    a wrong number on a dashboard. `items_seen` reading 0 where the walk merged
    thousands of states is a misreport an operator can discount; `position`
    reading 0 is an instruction, and the next attempt obeys it by walking the
    library from page one. So the handler below evolves `progress.run`, or the
    resume is a restart wearing a checkpoint's name.
    """

    __slots__ = ("run",)

    def __init__(self, run: SyncRun) -> None:
        self.run = run


def _watch_target(target: MediaItemTarget) -> MediaItemTarget | None:
    """What a `MediaItem` is matched to, collapsed to what a watch state carries.

    `None` if it is matched to nothing.

    An episode's row holds its series' `title_id` *and* its `episode_id`,
    because a client browsing a season wants both. `watch_states` permits
    exactly one (`num_nonnulls(title_id, episode_id) = 1`), so this is where the
    pair becomes a target, and the episode wins.

    Both alternatives are real failures rather than style. Passing the pair
    through raises `PortDataMalformed` by contract, which aborts the whole
    batch; passing the *title* merges every episode of a show onto one row,
    quietly.
    """
    if target.episode_id is not None:
        return MediaItemTarget(title_id=None, episode_id=target.episode_id)
    if target.title_id is not None:
        return target
    return None


class WatchStateSyncService:
    def __init__(
        self,
        media_items: MediaItemRepository,
        watch_states: WatchStateRepository,
        runs: SyncRunRepository,
        queue: JobQueue,
        commit: Callable[[], Awaitable[None]],
        *,
        batch_size: int = 1_000,
    ) -> None:
        self._media_items = media_items
        self._watch_states = watch_states
        self._runs = runs
        self._queue = queue
        self._commit = commit
        self._batch_size = batch_size

    async def sync(self, source: Source, adapter: SourceAdapter, *, user_id: uuid.UUID) -> SyncRun:
        """Walk this source's watch state into the catalog.

        Never raises a `UsherPortError`.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.watch_state") as span:
            span.set_attribute("usher.source", source.name)
            # This attempt's own instant, bound once. It is the fresh run's
            # `started_at` *and* every merge's `observed_at`: the same thing on a
            # first attempt and deliberately not on a resumed one, where the row
            # keeps the instant the logical walk began while the merges carry the
            # instant this attempt began.
            attempt_started = datetime.now(UTC)
            # The newest incomplete run is resumed in place: its id, its `cursor_at`
            # and -- load-bearing -- its `started_at`, so that when the walk finally
            # completes, `latest_completed_cursor` reads an instant covering
            # everything saved since the logical walk *began*.
            incomplete = await self._runs.latest_incomplete_run(source.id, SyncRunKind.WATCH_STATE)
            if incomplete is None:
                cursor = await self._runs.latest_completed_cursor(
                    source.id, SyncRunKind.WATCH_STATE
                )
                run = SyncRun(
                    source_id=source.id,
                    kind=SyncRunKind.WATCH_STATE,
                    cursor_at=cursor,
                    started_at=attempt_started,
                )
                # Committed `RUNNING` before the walk: an operator watching a
                # long sync needs a row to watch, and a killed process must
                # leave a trace rather than nothing.
                await self._runs.add(run)
            else:
                cursor = incomplete.cursor_at
                # `error` and `finished_at` cleared, so a resumed run does not read
                # as one that already ended.
                run = incomplete.evolve(status=SyncRunStatus.RUNNING, error=None, finished_at=None)
                await self._runs.save(run)
            # What this attempt inherited, for the telemetry below only.
            # `_walk` reads the resume point off `progress.run` rather than
            # taking it from here: two bindings for one number is exactly
            # what `_Progress` exists to prevent, and the walk's copy is the
            # one that moves.
            inherited, resumed_from = run.items_seen, run.position
            span.set_attribute("usher.resumed_from", resumed_from)
            await self._commit()
            progress = _Progress(run)
            try:
                await self._walk(progress, source.id, adapter, cursor, user_id, attempt_started)
                run = progress.run.evolve(
                    status=SyncRunStatus.COMPLETED, finished_at=datetime.now(UTC)
                )
            except UsherPortError as exc:
                # `progress.run`, never the pre-walk `run` -- see `_Progress`.
                run = progress.run.evolve(
                    status=SyncRunStatus.FAILED,
                    # str(exc), never the exception or a payload: PRD 08's
                    # credentials-are-never-logged rule applies to a Text
                    # column an operator reads.
                    error=str(exc),
                    finished_at=datetime.now(UTC),
                )
                span.set_attribute("usher.failed", True)
                # Both counts, because the run's is cumulative and reading it as
                # this attempt's is how a stalled resume looks healthy: an operator
                # watching one number climb across attempts cannot tell a walk that
                # is converging from one re-walking the same page forever.
                logger.error(
                    "watch-state sync of {source} failed after {attempt} states this attempt "
                    "({total} for the run, resumed from {resumed_from}): {error}",
                    source=source.name,
                    attempt=run.items_seen - inherited,
                    total=run.items_seen,
                    resumed_from=resumed_from,
                    error=str(exc),
                )
            await self._runs.save(run)
            await self._commit()
            span.set_attribute("usher.items_seen", run.items_seen)
            span.set_attribute("usher.items_unmatched", run.items_unmatched)
        _run_duration.record(
            time.perf_counter() - started, {"source": source.name, "status": run.status.value}
        )
        return run

    async def backfill_one(
        self, source: Source, adapter: SourceAdapter, *, external_id: str, user_id: uuid.UUID
    ) -> bool:
        """Ask the source for one item's authoritative state and merge it."""
        targets = await self._media_items.resolve_targets(source.id, [external_id])
        stored = targets.get(external_id)
        target = None if stored is None else _watch_target(stored)
        if target is None:
            logger.debug(
                "watch-history backfill skipped {external_id}: not matched on {source}",
                external_id=external_id,
                source=source.name,
            )
            return False
        state = await adapter.get_watch_state(external_id)
        if state is None:
            logger.debug(
                "watch-history backfill skipped {external_id}: {source} no longer has it",
                external_id=external_id,
                source=source.name,
            )
            return False
        await self._watch_states.merge_from_source(
            [self._merge_for(state, target, user_id, datetime.now(UTC))]
        )
        _backfilled.add(1, {"source": source.name})
        return True

    async def backfill_history(
        self, source: Source, adapter: SourceAdapter, *, limit: int = 500
    ) -> int:
        """One bounded pass over the rows that are played with no known count.

        Returns how many were recovered.
        """
        rows = await self._watch_states.list_needing_history(limit=limit)
        if not rows:
            return 0
        wanted = [
            MediaItemTarget(title_id=title_id, episode_id=episode_id)
            for _, title_id, episode_id in rows
        ]
        external_ids = await self._media_items.resolve_external_ids(source.id, wanted)
        recovered = 0
        for owner, title_id, episode_id in rows:
            external_id = external_ids.get(
                MediaItemTarget(title_id=title_id, episode_id=episode_id)
            )
            if external_id is None:
                # This source does not hold the item behind that row --
                # ordinary in a household with two sources.
                continue
            if await self.backfill_one(source, adapter, external_id=external_id, user_id=owner):
                await self._commit()
                recovered += 1
        return recovered

    async def _walk(
        self,
        progress: _Progress,
        source_id: uuid.UUID,
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        user_id: uuid.UUID,
        observed_at: AwareDatetime,
    ) -> None:
        """The nightly walk.

        It invalidates no rows and publishes no `row.invalidated`, and this is
        the place somebody would add both.
        """
        batch: list[SourceWatchState] = []
        seen = start_index = progress.run.position
        async for state in adapter.watch_state(since=cursor, start_index=start_index):
            batch.append(state)
            seen += 1
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(
                    progress.run, source_id, batch, user_id, observed_at, position=seen
                )
                batch = []
        if batch:
            # The trailing partial batch. A walk's count is almost never a
            # multiple of the batch size, so omitting this drops the last
            # page of nearly every run -- here, a household's most recent
            # resume positions.
            progress.run = await self._flush(
                progress.run, source_id, batch, user_id, observed_at, position=seen
            )

    async def apply_states(
        self,
        source_id: uuid.UUID,
        states: Sequence[SourceWatchState],
        *,
        user_id: uuid.UUID,
        observed_at: AwareDatetime,
    ) -> MergeOutcome:
        """Merge a batch of inbound watch state.

        Does not commit.
        """
        # One resolve for the batch, never one per state: `watch_state()` yields
        # one record per item, and a household has one item per file.
        targets = await self._media_items.resolve_targets(
            source_id, [state.external_id for state in states]
        )
        merges: list[WatchStateMerge] = []
        applied: list[MergedState] = []
        needing_history: list[str] = []
        unmatched = 0
        for state in states:
            stored = targets.get(state.external_id)
            target = None if stored is None else _watch_target(stored)
            if target is None:
                # An item in the review queue.
                unmatched += 1
                continue
            merges.append(self._merge_for(state, target, user_id, observed_at))
            applied.append(MergedState(external_id=state.external_id, target=target))
            if state.played and state.play_count is None:
                needing_history.append(state.external_id)
        rows_written = await self._watch_states.merge_from_source(merges) if merges else 0
        await self._enqueue_backfills(needing_history)
        return MergeOutcome(
            merged=tuple(applied),
            unmatched=unmatched,
            rows_written=rows_written,
            needing_history=tuple(needing_history),
        )

    async def _flush(
        self,
        run: SyncRun,
        source_id: uuid.UUID,
        batch: Sequence[SourceWatchState],
        user_id: uuid.UUID,
        observed_at: AwareDatetime,
        *,
        position: int,
    ) -> SyncRun:
        outcome = await self.apply_states(
            source_id, batch, user_id=user_id, observed_at=observed_at
        )
        run = run.evolve(
            items_seen=run.items_seen + len(batch),
            # `len(outcome.merged)`, never `outcome.rows_written`: this
            # column has always meant "states this walk had somewhere to
            # put", and a merge refused by "latest `updated_at` wins" is
            # still one of those.
            items_matched=run.items_matched + len(outcome.merged),
            items_unmatched=run.items_unmatched + outcome.unmatched,
            # Committed progress, saved with the batch it describes: a crash
            # re-walks the batch in flight and nothing before it.
            position=position,
        )
        await self._runs.save(run)
        # One commit per batch, exactly like `ReconcileService`: a crash
        # costs the batch in flight, never the walk.
        await self._commit()
        return run

    def _merge_for(
        self,
        state: SourceWatchState,
        target: MediaItemTarget,
        user_id: uuid.UUID,
        observed_at: AwareDatetime,
    ) -> WatchStateMerge:
        """The one place a `SourceWatchState` becomes a `WatchStateMerge`.

        `play_count`/`last_played_at` are copied as they are, `None` included:
        `None` reaches a `COALESCE` and leaves the stored value alone, `0` is a
        positive claim that the source reset it and is written. There is no
        default to fall back on and no `or 0` to add.

        `state.source_user_id` is deliberately not consulted. There is one user
        (PRD 01's authentication seam); mapping a source's user ids onto Usher's
        is a later question, and guessing here would put a second account's
        history on the singleton.
        """
        return WatchStateMerge(
            user_id=user_id,
            title_id=target.title_id,
            episode_id=target.episode_id,
            position_seconds=state.position_seconds,
            played=state.played,
            # A source's watch record carries no runtime; `MediaItem` does,
            # and the merge's `COALESCE` leaves a stored one alone.
            runtime_seconds=None,
            observed_at=observed_at,
            play_count=state.play_count,
            last_played_at=state.last_played_at,
        )

    async def _enqueue_backfills(self, external_ids: Sequence[str]) -> None:
        """One `enqueue` per batch for the played items whose count the walk could not report.

        `BACKFILL` priority, so recovering history never overtakes work a
        client is waiting on. `(kind, key)` is unique, so an item seen by
        five nightly walks before a worker reaches it is one row rather than
        five -- and re-enqueueing does not reset `created_at`, so it keeps
        its place in the age tiebreak.
        """
        if not external_ids:
            return
        traceparent = current_traceparent()
        await self._queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.WATCH_HISTORY,
                    key=external_id,
                    priority=JobPriority.BACKFILL,
                    traceparent=traceparent,
                )
                for external_id in external_ids
            ]
        )
