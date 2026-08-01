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

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.source import Source
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceWatchState,
)
from usher.services.ingest import IngestService
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
        max_items_per_event: int = 50,
    ) -> None:
        self._ingest = ingest
        self._watch = watch
        self._events = events
        self._commit = commit
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
            await self._publish_watch_states(states, outcome.merged, observed_at)
        return PushOutcome(states_merged=outcome.rows_written)

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
