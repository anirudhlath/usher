"""Inbound watch state (PRD 03), and the backfill ADR-0014 leaves behind.

**The one rule this whole milestone was handed, expressed as code that
cannot break it.** A source's *listing* frequently cannot report play
history -- verified against Emby 4.9.5.0, where a listing says
`PlayCount: 0` for an item played twice -- so `SourceWatchState.play_count`
is `int | None` and `None` means "this read could not determine it".
Everything below carries that `None` through **unchanged**, into
`WatchStateMerge`, into a `COALESCE`d `UPDATE`, and never through a
`x or 0`. A single `or 0` anywhere on this path replaces the household's
real history with zeros on every nightly walk, silently and permanently.

**A walk resolves; a backfill asks.** The walk merges what it can determine
and enqueues a `WATCH_HISTORY` job for every played item whose count it
could not, at background priority. That predicate is what keeps the
recovery bounded: `played` is the household's watched items -- thousands --
rather than the source's 1,126,674, and one upstream request per item at
the 1-5 s PRD 01 measures is a week for the library and an afternoon for
the household.

**There is no sweep.** `ReconcileService` retracts availability after a walk
that provably finished; nothing here ever retracts anything. PRD 08 lists
watch state as the precious set that survives everything, and an item that
vanished from a source is exactly the case where its stored position is the
only copy left.

Three shapes are borrowed from `ReconcileService` deliberately rather than
reinvented: the `RUNNING` row committed before the walk, the commit per
batch, and `_Progress` -- which exists because `SyncRun` is frozen and a
failure handler that evolves its own pre-walk binding writes `items_seen =
0` over a checkpoint that recorded eight.

`commit` is injected because `services/` may depend only on `domain/` and
`ports/` (ADR-0009), and a session is neither.
"""

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

    **The pair travels together because separating it is a real bug, found
    by the M5 plan's own self-review.** The push lane publishes one client
    event per merged state, carrying that state's position and played flag
    against that state's target. Recovering the pairing outside this service
    -- by zipping the targets against the batch the caller handed in --
    mis-pairs the moment the batch contains one unmatched item, because the
    targets are only the matched subset and `zip` aligns by position. It
    then publishes item A's resume position under item B's title id, which
    a client renders.

    Same rule `SourceEvent` states for `watch_states` one layer up, arrived
    at from the other side: keyed, never aligned by position.
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
    `played and play_count is None` would be a second copy of ADR-0014's
    predicate, and two copies is how they come to disagree.
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

    **Since #41 that checkpoint carries `position` too, and regressing
    *that* is no longer a wrong number on a dashboard.** `items_seen`
    reading 0 where the walk merged 5,000 states is a misreport an operator
    can discount; `position` reading 0 is an instruction, and the next
    attempt obeys it by walking the library from page one -- which is the
    loop ADR-0042 exists to close, restored by the one binding a failure
    handler is most likely to reach for. So the two failures this holder
    prevents are now one failure, and the handler below evolves
    `progress.run` or the resume is a restart wearing a checkpoint's name.
    """

    __slots__ = ("run",)

    def __init__(self, run: SyncRun) -> None:
        self.run = run


def _watch_target(target: MediaItemTarget) -> MediaItemTarget | None:
    """Collapse what a `MediaItem` is matched to into what a watch state may
    carry, or `None` if it is matched to nothing.

    An episode's row holds its series' `title_id` **and** its `episode_id`,
    because a client browsing a season wants both. `watch_states` permits
    exactly one (`num_nonnulls(title_id, episode_id) = 1`), so this is where
    the pair becomes a target, and the episode wins.

    Both alternatives are real failures rather than style. Passing the pair
    through raises `PortDataMalformed` by contract, which aborts a batch of
    five thousand states over 89% of this library; passing the *title*
    merges every episode of a show onto one row, quietly.
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
        """Walk this source's watch state into the catalog. Never raises a
        `UsherPortError`.

        Always incremental from the last *completed* watch-state run. This
        lane owns its own cursor: it walks a different method under a
        different upstream filter (`MinDateLastSavedForUser`, measured as
        genuinely different from the item lane's `MinDateLastSaved` -- 29,005
        against 28,934 items over the same 30-day window), so a cursor
        borrowed from a `FULL` or `DELTA` run would skip whatever changed in
        between. Unlike the item lanes there is no "full" variant to protect,
        because nothing here retracts.

        **"The last completed run" was, on its own, a premise that could not
        be earned** (#41). With none completed there is no cursor, so the
        walk is the whole library -- ~1.14M items, ~5,688 pages, about eleven
        hours -- and one transient failure anywhere in it records `FAILED`,
        which leaves no completed run, which leaves no cursor, which starts
        the same walk again at page one. It never once finished on the
        measured deployment, and the only remedy the operator had was to
        stop the entire worker.

        So an attempt no longer begins by minting a row. It **reclaims the
        newest incomplete one** and resumes at that row's `position`, so a
        failure costs the page in flight rather than the walk. The walk is
        not shorter; it is finishable, which it was not.

        Refusing the cursorless walk was the other candidate, and it is what
        the push lane does one lane over (`USHER_PUSH_GAP_CLOSE=cursored`,
        issue #9). It is wrong here for a reason that has nothing to do with
        taste: this method is never independently triggerable -- it is the
        unconditional second half of every `sync` job -- so a refusal would
        have no way to be lifted, because lifting it needs the first
        completed run that the refusal is what prevents.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.watch_state") as span:
            span.set_attribute("usher.source", source.name)
            # **The newest incomplete run is resumed in place** (#41,
            # ADR-0042): its id, its `cursor_at` and -- load-bearing -- its
            # `started_at`, so that when the walk finally completes,
            # `latest_completed_cursor` reads an instant covering everything
            # saved since the logical walk *began*. A fresh `started_at` per
            # attempt would skip whatever changed between the first attempt
            # and the last.
            resuming = await self._runs.latest_incomplete_run(source.id, SyncRunKind.WATCH_STATE)
            if resuming is None:
                cursor = await self._runs.latest_completed_cursor(
                    source.id, SyncRunKind.WATCH_STATE
                )
                run = SyncRun(source_id=source.id, kind=SyncRunKind.WATCH_STATE, cursor_at=cursor)
                # Committed `RUNNING` before the walk: an operator watching a
                # long sync needs a row to watch, and a killed process must
                # leave a trace rather than nothing.
                await self._runs.add(run)
            else:
                cursor = resuming.cursor_at
                run = resuming.evolve(status=SyncRunStatus.RUNNING, error=None, finished_at=None)
                await self._runs.save(run)
            start_index = run.position
            span.set_attribute("usher.resumed_from", start_index)
            await self._commit()
            progress = _Progress(run)
            try:
                await self._walk(progress, source.id, adapter, cursor, user_id, start_index)
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
            span.set_attribute("usher.items_seen", run.items_seen)
            span.set_attribute("usher.items_unmatched", run.items_unmatched)
        _run_duration.record(
            time.perf_counter() - started, {"source": source.name, "status": run.status.value}
        )
        return run

    async def backfill_one(
        self, source: Source, adapter: SourceAdapter, *, external_id: str, user_id: uuid.UUID
    ) -> bool:
        """Ask the source for one item's authoritative state and merge it.

        The expensive half of ADR-0014, and the handler behind a
        `WATCH_HISTORY` job. Returns whether anything was merged.

        Resolves the target *before* asking the source: an unmatched item
        has nowhere for the answer to land, and PRD 01 measures a
        single-item request at 1-5 s against one indexed read here.

        Quiet on both misses. `get_watch_state` answering `None` means the
        source no longer has the item -- which is the reconcile lane's
        problem, not this one's -- and parking a job for every deleted item
        fills the poison list with things that are simply gone.

        **`observed_at` is now, and that is load-bearing.** PRD 03's "latest
        `updated_at` wins" applies to the whole record, so a backfill
        carrying the walk's instant would be refused by the very row it is
        meant to repair: `watch_states` has a `BEFORE UPDATE` trigger that
        stamps the *write* instant, so a row the walk just merged already
        reads back an `updated_at` at or after anything that walk could
        hand over. The recovery would write nothing, the row would keep
        matching `played AND play_count = 0`, and the backfill would never
        converge -- one upstream request per item per sweep, forever.
        """
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
        """One bounded pass over the rows that are played with no known
        count. Returns how many were recovered.

        Bounded twice over: by `limit`, and by the predicate itself, which
        is the household's watched items rather than the source's
        1,126,674. Ordered oldest-first by `list_needing_history`, so a
        population larger than one pass drains across passes instead of
        re-reading the newest rows forever.

        Each row is written back to **its own** user, never to a single
        caller-supplied one: a second household member's history landing on
        the first is the sort of corruption that only appears once there are
        two of them.

        Committed per row. One upstream request per row already dominates,
        so the commit is free by comparison, and a backfill killed halfway
        keeps everything it recovered.

        This is the sweep, not the main path. The walk enqueues a
        `WATCH_HISTORY` job for each of these as it sees them, and a job for
        a deleted item completes and disappears; a row this pass cannot
        answer stays in the predicate and costs one request per pass, which
        is why the pass is bounded rather than a `while`.
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
        start_index: int,
    ) -> None:
        """The nightly walk. **It invalidates no rows and publishes no
        `row.invalidated`, and this is the place somebody would add both.**

        A walk merges up to 1,126,789 states. One `RowCache.invalidate` per
        merged row is the fan-out per row per night that PRD 07 already refuses
        for `watchstate.updated` -- and with `row.invalidated` attached it is
        that fan-out reaching every connected client *and* instructing each one
        to refetch, which is a thundering herd at 04:00 on top of it. Nothing
        else would notice: the cache would be correct, the screens would be
        fresh, and the only symptom is a million-message night.

        The walk's changes reach the screen through the composed screen's own
        30 s TTL and a demand read. A walk that finished at 04:00 is on the
        screen by 04:00:30, which is the honest and entirely adequate answer.
        The push lane is where invalidation belongs, because a push event *is*
        a change -- `services/push.py::_invalidate_rows`.

        `start_index` is where a resumed attempt picks up -- the position the
        last attempt *committed*, so the batch that was in flight when it
        died is re-walked. That is free: every write here is an idempotent
        upsert and this lane retracts nothing.
        """
        batch: list[SourceWatchState] = []
        seen = start_index
        async for state in adapter.watch_state(since=cursor, start_index=start_index):
            batch.append(state)
            seen += 1
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(progress.run, source_id, batch, user_id, seen)
                batch = []
        if batch:
            # The trailing partial batch. A walk's count is almost never a
            # multiple of the batch size, so omitting this drops the last
            # page of nearly every run -- here, a household's most recent
            # resume positions.
            progress.run = await self._flush(progress.run, source_id, batch, user_id, seen)

    async def apply_states(
        self,
        source_id: uuid.UUID,
        states: Sequence[SourceWatchState],
        *,
        user_id: uuid.UUID,
        observed_at: AwareDatetime,
    ) -> MergeOutcome:
        """Merge a batch of inbound watch state. **Does not commit.**

        Extracted from `_flush` with no behaviour change, because the push
        lane needs exactly this chain and reimplementing any link of it
        would reimplement the failure ADR-0014 exists to prevent: one
        batched `resolve_targets`, the episode-wins collapse, unmatched
        counted rather than raised, `play_count`/`last_played_at` copied as
        they are with `None` included, and a `WATCH_HISTORY` job for every
        played item whose count the read could not determine.

        `observed_at` is the caller's: a walk passes its run's start instant
        (so the sweep's arithmetic and PRD 03's conflict rule both hold over
        a walk that takes hours), and the push lane passes the instant the
        event arrived. Neither may pass `now()` from inside this method -- a
        per-row write instant is a different quantity that happens to
        compare the same way, and nothing downstream can recover the first
        from it.

        The commit is the caller's too, for the reason it is everywhere else
        in `services/`: a walk commits per batch and the push lane commits
        per event, and those are different units of work.
        """
        # One resolve for the batch, never one per state: `watch_state()`
        # yields one record per item and this deployment has 1,126,674.
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
                # An item in the review queue. Counted rather than raised
                # on: `merge_from_source` answers a target-less merge with
                # `PortDataMalformed`, which would abort the whole batch
                # over one unresolved item -- and PRD 02's "unmatched items
                # are never dropped" means there will always be some. On the
                # push lane the same raise costs a reconnect and a
                # gap-closing delta walk.
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
        position: int,
    ) -> SyncRun:
        outcome = await self.apply_states(
            source_id, batch, user_id=user_id, observed_at=run.started_at
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

        `play_count`/`last_played_at` are copied **as they are, `None`
        included**. That is the whole of ADR-0014 at this layer: `None`
        reaches a `COALESCE` and leaves the stored value alone, `0` is a
        positive claim that the source reset it and is written. There is no
        default to fall back on and no `or 0` to add.

        `state.source_user_id` is deliberately not consulted. M4 has one
        user (PRD 01's authentication seam); mapping a source's user ids
        onto Usher's is M5's, and guessing here would put a second Emby
        account's history on the singleton.
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
        """One `enqueue` per batch for the played items whose count the walk
        could not report.

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
