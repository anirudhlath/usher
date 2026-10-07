"""Inbound watch state (PRD 03), and the backfill it leaves behind."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import Source
from usher.domain.sync import ABANDONED_ERROR, SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import UsherPortError
from usher.ports.ingest import MediaItemTarget, WatchStateMerge
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import MediaItemRepository, SyncRunRepository, WatchStateRepository
from usher.ports.source import SourceAdapter, SourceWatchState
from usher.services.run_closing import close_cancelled
from usher.telemetry import current_traceparent

_tracer = trace.get_tracer("usher.watch_sync")
_meter = metrics.get_meter("usher.watch_sync")
_run_duration = _meter.create_histogram(
    "usher.watch_state.run.duration", unit="s", description="Wall time per watch-state run"
)
_backfilled = _meter.create_counter(
    "usher.watch_state.backfilled", unit="1", description="Play histories recovered by a backfill"
)


def _now() -> datetime:
    return datetime.now(UTC)


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
    changed -- the two differ whenever PRD 03's "latest wins" refuses one,
    and `SyncRun.items_matched` has always meant the first.
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


@dataclass(frozen=True, slots=True)
class _Read:
    """What the reader hands the walk: a state, the listing's end, or its error."""

    state: SourceWatchState | None = None
    error: Exception | None = None


class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose, and for the reason `ReconcileService._Progress`
    states at length: `SyncRun` is frozen, `_flush` saves an evolved copy
    per batch, and a failure handler holding the pre-walk value regresses
    the durable checkpoint to zero on every failure.

    A watch run's `position` counts the states it has committed, and nothing
    reads it back: a watch run is never resumed, and a failed one hands the next
    only its cursor.
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
        rollback: Callable[[], Awaitable[None]],
        *,
        batch_size: int = 1_000,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        # `not … > 0` rather than `<= 0`, which a NaN would pass.
        if not heartbeat_seconds > 0:
            raise ValueError(
                f"a heartbeat needs a positive period, not {heartbeat_seconds} seconds"
            )
        self._media_items = media_items
        self._watch_states = watch_states
        self._runs = runs
        self._queue = queue
        self._commit = commit
        self._rollback = rollback
        self._batch_size = batch_size
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock

    async def sync(
        self,
        source: Source,
        adapter: SourceAdapter,
        *,
        user_id: uuid.UUID,
        since_at_most: AwareDatetime | None = None,
        beat: Callable[[], Awaitable[object]] | None = None,
    ) -> SyncRun:
        """Walk this source's watch state into the catalog.

        Each run starts afresh, after closing this source's dead watch runs: none is
        resumed or superseded. It reads from the oldest of the lane's cursor, the cursor
        of every failed run no completed run has covered yet -- by starting no earlier
        and reading from no later, both with a cursor or both without -- and
        `since_at_most`, so a caller can cover what its item walk stored after this
        lane's last run began. A run another process is still walking is left to it, and
        this walk runs beside it in a row of its own. `beat` is awaited after each batch
        this walk commits and each beat of its own heartbeat, due `heartbeat_seconds`
        after its last commit or beat, so a caller whose own run waits on this walk can
        keep that run's heartbeat moving, a page under retry included. `beat` must not
        raise a `UsherPortError`, which would be recorded as this walk's failure.
        Cancelled once its run has committed, the walk closes that run with
        `close_cancelled` before the cancellation propagates. Never raises a
        `UsherPortError`.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.watch_state") as span:
            span.set_attribute("usher.source", source.name)
            # This attempt's instant, bound once: the run's `started_at` and every
            # merge's `observed_at`.
            attempt_started = datetime.now(UTC)
            now = self._clock()
            # A dead run's row is closed, never resumed: a `StartIndex` into a listing
            # that has lost items since would skip some. A live one is another
            # process's, and this run walks beside it.
            closed = await self._runs.close_abandoned(
                source.id, (SyncRunKind.WATCH_STATE,), now=now, error=ABANDONED_ERROR
            )
            if closed:
                logger.warning(
                    "closed {count} watch-state run(s) of {source} whose process had stopped",
                    count=closed,
                    source=source.name,
                )
            cursor = await self._runs.latest_completed_cursor(source.id, SyncRunKind.WATCH_STATE)
            # A failed run's window is read again until a run that covers it completes,
            # the runs just closed among them. `None`, from the beginning, is the oldest.
            for carried in await self._runs.uncovered_failed_cursors(
                source.id, SyncRunKind.WATCH_STATE
            ):
                cursor = None if cursor is None or carried is None else min(cursor, carried)
            if cursor is not None and since_at_most is not None:
                # Never past the instant the caller's item walk began: a state saved
                # since then may be for an item that walk had not yet stored.
                cursor = min(cursor, since_at_most)
            run = SyncRun(
                source_id=source.id,
                kind=SyncRunKind.WATCH_STATE,
                cursor_at=cursor,
                started_at=attempt_started,
                heartbeat_at=now,
            )
            # Committed `RUNNING` before the walk, with the close above: an operator
            # watching a long sync needs a row to watch, and a killed process must leave
            # a trace rather than nothing.
            await self._runs.add(run)
            run_id = run.id
            try:
                await self._commit()
                progress = _Progress(run)
                try:
                    await self._walk(
                        progress, source.id, adapter, cursor, user_id, attempt_started, beat
                    )
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
                    logger.error(
                        "watch-state sync of {source} failed after {seen} states: {error}",
                        source=source.name,
                        seen=run.items_seen,
                        error=str(exc),
                    )
                await self._runs.save(run)
                await self._commit()
            except asyncio.CancelledError:
                # Closed as last committed, so the next walk does not take it for a live one.
                await close_cancelled(
                    self._runs,
                    run_id,
                    rollback=self._rollback,
                    commit=self._commit,
                    source=source.name,
                )
                raise
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
                "watch-history backfill skipped {external_id}: {source} no longer has it, "
                "or reported a state that cannot be stored",
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
        beat: Callable[[], Awaitable[object]] | None,
    ) -> None:
        """The nightly walk: a reader lists the states while this task merges them.

        Only this task touches the session. A beat -- the run's heartbeat alone, saved
        and committed, then `beat` -- falls due `heartbeat_seconds` after the last
        commit or beat, so a page riding out its retries leaves the run alive. The
        reader's error is raised after every state it read ahead of it, with the
        partial batch uncommitted. However the walk ends, the reader is cancelled and
        awaited first.

        It invalidates no rows and publishes no `row.invalidated`, and this is
        the place somebody would add both.
        """
        queue: asyncio.Queue[_Read] = asyncio.Queue(maxsize=self._batch_size)
        reader = asyncio.create_task(self._read(adapter, cursor, 0, queue))
        try:
            batch: list[SourceWatchState] = []
            seen = 0
            # A deadline, not a silence: states that keep arriving below a batch would
            # otherwise hold every beat off.
            due = time.monotonic() + self._heartbeat_seconds
            while True:
                if time.monotonic() >= due:
                    progress.run = await self._checkpoint(progress.run, beat)
                    due = time.monotonic() + self._heartbeat_seconds
                    continue
                try:
                    read = await asyncio.wait_for(queue.get(), max(0.0, due - time.monotonic()))
                except TimeoutError:
                    continue
                if read.error is not None:
                    raise read.error
                if read.state is None:
                    break
                batch.append(read.state)
                seen += 1
                if len(batch) >= self._batch_size:
                    progress.run = await self._flush(
                        progress.run,
                        source_id,
                        batch,
                        user_id,
                        observed_at,
                        position=seen,
                        beat=beat,
                    )
                    batch = []
                    due = time.monotonic() + self._heartbeat_seconds
            if batch:
                # The trailing partial batch. A walk's count is almost never a
                # multiple of the batch size, so omitting this drops the last
                # page of nearly every run -- here, a household's most recent
                # resume positions.
                progress.run = await self._flush(
                    progress.run, source_id, batch, user_id, observed_at, position=seen, beat=beat
                )
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @staticmethod
    async def _read(
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        start_index: int,
        queue: asyncio.Queue[_Read],
    ) -> None:
        """The reader: put each state the listing yields on `queue`, then its end.

        It never touches the database. An error the listing raises goes on the
        queue in place of the end, and the reader stops.
        """
        try:
            states = adapter.watch_state(since=cursor, start_index=start_index)
            async with aclosing(states):
                async for state in states:
                    await queue.put(_Read(state))
        except Exception as exc:
            # Nothing reads the queue once the reader is cancelled, so an error the
            # listing's close raised must not take the cancellation's place.
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise asyncio.CancelledError from exc
            await queue.put(_Read(error=exc))
            return
        await queue.put(_Read())

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
        # One resolve for the batch, never one per state: a first walk yields one
        # record per item the account has watched, and a delta one per change.
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
        beat: Callable[[], Awaitable[object]] | None,
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
            # The states this run has committed, saved with the batch that ends them.
            position=position,
        )
        # One commit per batch, exactly like `ReconcileService`: a crash loses the
        # batch in flight, never the merges committed before it.
        return await self._checkpoint(run, beat)

    async def _checkpoint(
        self, run: SyncRun, beat: Callable[[], Awaitable[object]] | None
    ) -> SyncRun:
        """Save `run` with a new heartbeat and commit it, then await `beat` if given."""
        run = run.evolve(heartbeat_at=self._clock())
        await self._runs.save(run)
        await self._commit()
        if beat is not None:
            await beat()
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
