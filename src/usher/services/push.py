"""PRD 03's push lane: the fast path, and the reconnect that closes its gap.

Two things live here and they are separated because only one of them is hard
to test. `PushApplyService` turns one `SourceEvent` into catalog state and is
an ordinary service. `PushSupervisor` owns a long-lived connection, a backoff
schedule and a failure ceiling, and every one of those needs an injected
clock and an injected sleep to be testable at all.

**Neither of them decides whether push is healthy.** That answer comes from
`SourceAdapter.supports_push`, which every adapter grounds in *messages
received* rather than in a socket being open -- ADR-0004 measured a handshake
against a nonexistent path upgrading and being held open, so the connection
object existing is a state that must read `False`. The supervisor's job is to
*act* on that answer: reset its failure counter on evidence of delivery, and
after a ceiling of consecutive failures write `supports_push = false` on the
`Source` row, which is what hands the source back to the nightly reconcile.

**Nothing here re-implements the inbound merge.** A watch-state event lands
on `WatchStateSyncService.apply_states`, the same chain a walk uses, because
any second copy of it is a second chance to write a zero over real play
history (ADR-0014). Item events land on `IngestService.ingest_batch` for the
same reason.

`commit` and the three unit-of-work callables are injected because
`services/` may depend only on `domain/` and `ports/` (ADR-0009), and a
session is neither -- the same shape `ReconcileService` and `JobWorker`
already use.
"""

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.source import Source
from usher.ports.errors import UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceNotSupported,
    SourceWatchState,
)
from usher.services.ingest import IngestService
from usher.services.rows import WATCH_STATE_ROWS
from usher.services.rows.cache import RowCache
from usher.services.watch_sync import MergedState, WatchStateSyncService

_tracer = trace.get_tracer("usher.push")
_meter = metrics.get_meter("usher.push")
_events_applied = _meter.create_counter(
    "usher.source.push.events", unit="1", description="Push events applied, by source and kind"
)


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """What one event did.

    `deferred_to_delta` is the one field the supervisor branches on: an
    event naming more items than the lane will resolve one at a time is
    answered by a delta walk instead, which is one paged request per 200
    items rather than one request per item.
    """

    items_ingested: int = 0
    states_merged: int = 0
    ignored: int = 0
    deferred_to_delta: bool = False


class PushApplyService:
    def __init__(
        self,
        ingest: IngestService,
        watch: WatchStateSyncService,
        events: EventPublisher,
        commit: Callable[[], Awaitable[None]],
        *,
        cache: RowCache | None = None,
        max_items_per_event: int = 50,
    ) -> None:
        self._ingest = ingest
        self._watch = watch
        self._events = events
        self._commit = commit
        # **The push lane invalidates; the nightly walk expires.** A push event
        # *is* a change -- the same sentence PRD 07 uses to explain why this
        # lane publishes `watchstate.updated` and the walk does not -- so the
        # fan-out is per *event*, over a small fixed slug set, rather than per
        # merged row. `None` for a deployment composing no screens (the CLI's
        # own roots), where an invalidation would have no cache to reach.
        self._cache = cache
        self._max_items = max_items_per_event

    async def apply(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent, *, user_id: uuid.UUID
    ) -> PushOutcome:
        """Apply one push event. Commits once, and never raises for a
        missing item.

        The span is a child of whatever the lane has active, which is
        nothing -- a push lane has no request above it -- so this is a root
        per event. Deliberately not linked to anything: unlike a job, there
        is no enqueueing span to link *to*.
        """
        with _tracer.start_as_current_span(f"push.{event.kind.value}") as span:
            span.set_attribute("usher.source", source.name)
            span.set_attribute("usher.push.items", len(event.external_ids))
            if event.kind is SourceEventKind.WATCH_STATE_CHANGED:
                outcome = await self._apply_watch_state(source, adapter, event, user_id)
            elif event.kind is SourceEventKind.ITEM_REMOVED:
                outcome = self._ignore_removal(source, event)
            else:
                outcome = await self._apply_items(source, adapter, event)
            span.set_attribute("usher.push.deferred", outcome.deferred_to_delta)
        _events_applied.add(1, {"source": source.name, "kind": event.kind.value})
        return outcome

    async def _apply_watch_state(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent, user_id: uuid.UUID
    ) -> PushOutcome:
        carried = {state.external_id: state for state in event.watch_states}
        missing = [external_id for external_id in event.external_ids if external_id not in carried]
        if len(missing) > self._max_items:
            # No payload and too many to ask for one at a time. A delta walk
            # under `MinDateLastSavedForUser` covers the same window in
            # paged requests rather than per-item ones.
            logger.info(
                "{source} pushed {count} watch-state changes with no payload; "
                "deferring to a delta walk",
                source=source.name,
                count=len(missing),
            )
            return PushOutcome(deferred_to_delta=True)
        states: list[SourceWatchState] = []
        for external_id in event.external_ids:
            state = carried.get(external_id)
            if state is None:
                # One request, and it is the authoritative one -- so an
                # adapter with no payload is *more* accurate here, not less.
                state = await adapter.get_watch_state(external_id)
            if state is None:
                # The source no longer has it. The reconcile lane's problem,
                # and raising would cost a reconnect and a gap-closing walk.
                continue
            states.append(state)
        if not states:
            return PushOutcome()
        # `now()`, never the event's own timestamp or a run's instant:
        # PRD 03's "latest `updated_at` wins" covers the whole record and
        # `watch_states` has a `BEFORE UPDATE` trigger that stamps the write
        # instant, so an observation stamped earlier than the row it is
        # repairing writes nothing at all. Invisible against
        # `FakeWatchStateRepository`, which stores `observed_at` as
        # `updated_at` and therefore accepts what Postgres refuses --
        # `tests/integration/test_services_push.py` is what closes it.
        observed_at = datetime.now(UTC)
        outcome = await self._watch.apply_states(
            source.id, states, user_id=user_id, observed_at=observed_at
        )
        await self._commit()
        if outcome.rows_written:
            await self._invalidate_rows(user_id)
            await self._publish_watch_states(states, outcome.merged, observed_at)
        return PushOutcome(states_merged=outcome.rows_written)

    async def _invalidate_rows(self, user_id: uuid.UUID) -> None:
        """Drop this household's watch-state rows and its composed screen, and
        tell every connected client which rows to refetch.

        **Trap 5, on the right side of it.** The nightly walk merges up to
        1,126,789 states and invalidates *nothing*: one invalidation per merged
        row is the fan-out per row per night that PRD 07 already refuses for
        `watchstate.updated`, and the walk's changes reach the screen through
        the 30 s screen TTL and a demand read -- a walk that finishes at 04:00
        is on the screen by 04:00:30. Here the unit is one pushed event, whose
        slug set is `WATCH_STATE_ROWS` and is fixed.

        Guarded on `rows_written` for the reason the publish beside it is: a
        merge refused by "latest `updated_at` wins" is the source echoing back a
        position a client just set, and dropping a warm screen for it is a full
        recompose per second of playback.
        """
        if self._cache is not None:
            self._cache.invalidate(user_id, WATCH_STATE_ROWS)
        # **One event per invalidated slug, and no `title_id`.** PRD 07's
        # payload for this event is a row slug and its client action is
        # "refetch that row", so the slug is the whole payload -- a frame
        # without it is an instruction with no object. The absent `title_id` is
        # what makes this the one event the `?titles=` filter cannot express:
        # it reaches unfiltered subscribers and no others, which is correct,
        # because a client that sent `?titles=` is on a detail screen and a row
        # invalidation is exactly the unrelated churn that filter exists to
        # keep off it.
        #
        # Published here rather than beside the cache write inside `RowCache`,
        # because the cache is a dict and a dict that published events would be
        # a second publisher nobody could see from the lane that owns the bus.
        for slug in WATCH_STATE_ROWS:
            await self._events.publish(
                ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": slug})
            )

    async def _publish_watch_states(
        self,
        states: Sequence[SourceWatchState],
        merged: Sequence[MergedState],
        observed_at: datetime,
    ) -> None:
        """One `watchstate.updated` per state a merge was built for.

        **Keyed by `external_id`, never zipped.** `merged` is the *matched
        subset* of `states`, so pairing the two by position mis-pairs the
        moment the batch holds one unmatched item and publishes item A's
        resume position under item B's title id. That is the defect the M5
        plan's own self-review found in its draft of this method; the fix is
        that `apply_states` reports the pair it built rather than leaving it
        to be recovered here. Same rule `SourceEvent.watch_states` states
        one layer up, for the same reason.

        Published only when `rows_written` was non-zero, which is the
        repository's own count: a merge refused by "latest `updated_at`
        wins" is the source echoing back a position a client just set, and
        re-rendering a detail screen on every one of those is a flicker per
        second of playback.

        Slightly over-published in one direction, stated rather than hidden:
        a batch where three of five merges landed publishes all five,
        because `merge_from_source` returns a count and not a set. Correct
        to fix later; wrong to fix by publishing nothing.
        """
        by_id = {state.external_id: state for state in states}
        for entry in merged:
            state = by_id[entry.external_id]
            await self._events.publish(
                ClientEvent(
                    kind=ClientEventKind.WATCHSTATE_UPDATED,
                    title_id=entry.target.title_id,
                    episode_id=entry.target.episode_id,
                    data={
                        "position_seconds": state.position_seconds,
                        "played": state.played,
                        "observed_at": observed_at.isoformat(),
                    },
                )
            )

    async def _apply_items(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent
    ) -> PushOutcome:
        if len(event.external_ids) > self._max_items:
            logger.info(
                "{source} pushed {count} item changes; deferring to a delta walk",
                source=source.name,
                count=len(event.external_ids),
            )
            return PushOutcome(deferred_to_delta=True)
        items: list[SourceItem] = []
        for external_id in event.external_ids:
            item = await adapter.get_item(external_id)
            if item is not None:
                items.append(item)
        if not items:
            return PushOutcome()
        result = await self._ingest.ingest_batch(source.id, items, observed_at=datetime.now(UTC))
        await self._commit()
        for outcome in result.outcomes:
            if outcome.title_id is not None:
                await self._events.publish(
                    ClientEvent(
                        kind=ClientEventKind.TITLE_UPDATED,
                        title_id=outcome.title_id,
                        episode_id=outcome.episode_id,
                        data={"fields": ["availability"]},
                    )
                )
        return PushOutcome(items_ingested=len(items))

    def _ignore_removal(self, source: Source, event: SourceEvent) -> PushOutcome:
        """A removal event retracts nothing, and says so.

        [ADR-0015](../../../docs/prd/decisions/0015-availability-is-retracted-only-by-a-finished-walk.md):
        availability is retracted only by a walk that provably finished,
        because there is no way to tell a genuine deletion from an unmounted
        drive, a library removed by accident, or a permissions change -- and
        only one of those is reversible. Emby emits `ItemsRemoved` during an
        ordinary library refresh for items that have not gone anywhere.

        PRD 08 already prices the delay: "Availability goes stale, not
        wrong." Counted and logged rather than dropped silently, so an
        operator watching a source that really did lose a library can see
        the events arriving before the nightly walk acts on them.
        """
        logger.info(
            "{source} reported {count} items removed; availability is retracted only by a "
            "full walk (ADR-0015), so nothing changes until the nightly reconcile",
            source=source.name,
            count=len(event.external_ids),
        )
        return PushOutcome(ignored=len(event.external_ids))


# The three units of work a lane needs, each opening its own session. They
# are callables rather than services because a supervisor that held a
# session would hold it for the life of the socket -- hours, idle in
# transaction, with a snapshot from whenever the lane started. The
# composition root is where each one becomes
# `async with factory() as session: ...`, which is the same shape
# `usher.services.handlers.SourceResolver` already uses one milestone down.
#
# `user_id` is bound by the composition root into the applier rather than
# carried on the supervisor: the lane has no use for it, and an attribute
# that is stored and never read is a parameter every later caller has to
# guess the meaning of.
PushApplier = Callable[[Source, SourceAdapter, SourceEvent], Awaitable[PushOutcome]]
GapCloser = Callable[[Source, SourceAdapter], Awaitable[None]]
PushAvailabilityWriter = Callable[[Source, bool], Awaitable[None]]


class _Gate:
    """When the gap was last closed, for one run.

    A small mutable holder rather than an instance attribute, so one
    supervisor can safely run two sources -- the same reasoning
    `ReconcileService._Progress` states, arrived at from the other side.
    """

    __slots__ = ("at",)

    def __init__(self) -> None:
        self.at: float | None = None


class PushSupervisor:
    def __init__(
        self,
        apply: PushApplier,
        close_gap: GapCloser,
        set_push_available: PushAvailabilityWriter,
        *,
        max_consecutive_failures: int = 5,
        backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 300.0,
        gap_min_interval_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._apply = apply
        self._close_gap = close_gap
        self._set_push_available = set_push_available
        self._max_failures = max_consecutive_failures
        self._backoff_seconds = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._gap_min_interval = gap_min_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter

    async def run(self, source: Source, adapter: SourceAdapter) -> None:
        """Hold this source's push channel until it stops being worth
        holding. Returns; never raises a `UsherPortError`.

        Returns rather than looping forever, and that is PRD 08: "after N
        failures mark `supports_push = false` and lean on the nightly walk."
        A lane that retried indefinitely against a proxy stripping `Upgrade`
        would look identical to a working one from every dashboard.
        """
        gate = _Gate()
        failures = 0
        delivering = False
        while failures < self._max_failures:
            try:
                async with adapter.events() as events:
                    # **After** the connection, deliberately. Anything that
                    # changes during this walk arrives on the socket that is
                    # already open and is buffered; the reverse order leaves
                    # the window between the walk and the handshake silently
                    # uncovered. `connect_websocket`'s `max_queue=256` is the
                    # other half of the same decision.
                    await self._gap(source, adapter, gate)
                    delivering = await self._note(source, adapter, delivering)
                    async for event in events:
                        delivering = await self._note(source, adapter, delivering)
                        if delivering:
                            # Reset on **delivery**, never on connection. A
                            # proxy that upgrades and then buffers connects
                            # perfectly every time; if that reset the counter
                            # the lane would reconnect forever and the
                            # ceiling below would never be reached -- which
                            # is PRD 08's failure policy quietly not
                            # happening, on a source the reconciler has been
                            # told it does not need to cover.
                            failures = 0
                        outcome = await self._apply(source, adapter, event)
                        if outcome.deferred_to_delta:
                            await self._gap(source, adapter, gate)
            except SourceNotSupported as exc:
                # Not a failure to retry: an adapter with no channel will say
                # the same thing every time, and the reconciler is the cover
                # PRD 03 designed for exactly this.
                logger.info(
                    "{source} has no push channel ({error}); the reconciler covers it",
                    source=source.name,
                    error=str(exc),
                )
                await self._set_push_available(source, False)
                return
            except asyncio.CancelledError:
                # Shutdown is not a push failure. Marking the source here
                # would disable push on every source on every restart until a
                # walk re-enabled it. `CancelledError` is a `BaseException` in
                # 3.13 and the arm below would not catch it anyway -- this is
                # what stops a later reader widening that arm to
                # `except Exception` without noticing.
                raise
            except UsherPortError as exc:
                failures += 1
                delivering = False
                logger.warning(
                    "{source}'s push channel failed ({failures}/{ceiling}): {error}",
                    source=source.name,
                    failures=failures,
                    ceiling=self._max_failures,
                    error=str(exc),
                )
            else:
                # The port forbids an iterator that ends quietly, and an
                # adapter can still do it. Counted as a failure rather than
                # treated as a clean shutdown, because the alternative is a
                # lane that returns silently and a source that stops pushing
                # until somebody notices by hand.
                failures += 1
                delivering = False
                logger.warning(
                    "{source}'s push channel ended without raising ({failures}/{ceiling})",
                    source=source.name,
                    failures=failures,
                    ceiling=self._max_failures,
                )
            if failures < self._max_failures:
                await self._sleep(self._backoff(failures))
        logger.error(
            "{source}'s push channel failed {count} times in a row; marking it unavailable "
            "and leaving this source to the nightly reconcile",
            source=source.name,
            count=failures,
        )
        await self._set_push_available(source, False)

    async def _note(self, source: Source, adapter: SourceAdapter, was: bool) -> bool:
        """Read `supports_push` and persist the transition.

        The read is the adapter's, which grounds it in received messages
        rather than in a socket being open. This layer neither knows nor
        guesses -- and writing only on the *transition* keeps `sources` from
        taking one `UPDATE` per second of playback for a value that changed
        once.
        """
        now_delivering = adapter.supports_push
        if now_delivering != was:
            await self._set_push_available(source, now_delivering)
        return now_delivering

    async def _gap(self, source: Source, adapter: SourceAdapter, gate: _Gate) -> None:
        """PRD 03's reconnect delta, rate-limited.

        A flapping socket plus one delta per reconnect is a paged walk of
        everything changed since the cursor every few seconds. The first
        after a real outage is the expensive one and is never skipped,
        because `gate.at` is `None` until one has run.
        """
        now = self._clock()
        if gate.at is not None and now - gate.at < self._gap_min_interval:
            logger.debug(
                "skipping {source}'s gap-closing delta; one ran {ago:.0f}s ago",
                source=source.name,
                ago=now - gate.at,
            )
            return
        gate.at = now
        await self._close_gap(source, adapter)

    def _backoff(self, failures: int) -> float:
        """Equal jitter, exactly PRD 08's shape for the job queue.

        A uniform draw from `[base/2, base) x 2^(failures-1)`, capped. Not
        *full* jitter, whose minimum draw is arbitrarily close to zero, so a
        share of failures retry effectively immediately -- the hot loop the
        backoff exists to prevent, merely rationed. The spread is what breaks
        a thundering herd across sources; the half-interval floor is what
        makes "a failed connection is not instantly retried" a property
        rather than a probability.
        """
        base = min(self._backoff_seconds * (2 ** (failures - 1)), self._max_backoff)
        return self._jitter(base / 2, base)
