"""A `SourceAdapter` with no wire format at all, and its harness.

Exists to prove `SourceAdapterContract` is expressible without reference to
Emby. If the suite passes here *and* against `EmbyAdapter`, the assertions
are about the port; if it only passed against Emby, they would only be
about Emby.

Its round-trip cases are close to tautological -- it hands back the
`SourceItem`s it was seeded with. That is deliberate and not a defect: the
round-trip has teeth in `EmbyHarness`, where the same seeded item has to
survive being rendered into JSON and parsed back. What this fake models for
real is the two behaviours a no-op would let pass on *both* sides:

- a session token that can expire and must be silently re-minted, with
  concurrent expiries collapsing into a single authentication; and
- a rejected credential that is remembered, so a wrong password cannot turn
  every subsequent call into another doomed authentication.

Without those, `test_operations_recover_from_an_expired_credential` and
`test_rejected_credentials_do_not_produce_a_request_storm` would pass here
against an adapter that did nothing at all, and a reviewer would have no
signal that the assertions mean anything.

**Where this fake is more forgiving than a real source, on purpose.** Its
walk returns the *true* `play_count`/`last_played_at`, because it yields
back the very `SourceWatchState` the harness seeded. So
`test_a_walk_never_reports_play_history_it_cannot_know` passes here on its
`== 7` branch and never exercises the `is None` branch -- the branch that
matters, and the branch the measured Emby behaviour lands on. That case has
teeth only in `EmbyHarness`, where the fake server's *listing* renderer
omits the two fields exactly as Emby 4.9.5.0 does. The fake's job is to
prove the assertion is expressible without reference to Emby; it is not
evidence that any adapter needed it.

**Its push channel is the same kind of forgiving, and worse.** There is no
transport under it at all: no handshake, no frames, no close code, no
backpressure. Its health ledger decays only because a test advanced a clock
it owns, and it goes silent or drops because a test said so, so nothing
about a real socket's failure modes is expressible here. What it *is*
evidence for is that the six push contract cases are statable without a wire
format -- and that they are satisfiable by a second, independently written
three-clause health rule, which is the only sense in which "the port stated
the rule" is a testable claim rather than "`EmbyAdapter` happens to behave
that way". The same six run against the real `EmbyAdapter` over a real
`EmbyPushChannel` with a real watchdog
(`tests/unit/test_adapters_emby_contract.py`), and only M5's live
verification closes the gap after that.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from urllib.parse import quote

from pydantic import AwareDatetime

from tests.contract.source_harness import SourceHarness
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    StreamTargetKind,
    WatchStateUpdate,
)

# Same layout table the Emby mapper uses. Duplicated rather than imported so
# this fake stays independent of the adapter it is meant to be an
# alternative to -- importing Emby's mapper here would make "the suite is
# not Emby-shaped" untrue by construction.
_LAYOUTS = {1: "1_0", 2: "2_0", 6: "5_1", 8: "7_1"}

# Put on the queue by `drop_push` so a consumer parked on `get()` wakes and
# raises, rather than hanging until the case's own `wait_for` expires and
# reports a timeout where the real answer is a raise. `push_drop`'s contract
# is "promptly", and the flag alone could only be noticed on the next poll
# tick.
_DROPPED = SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("__dropped__",))

# How long one `get()` waits before the drain runs its own watchdog. Real
# wall time, and the only real wall time anywhere in this fake's push side --
# everything else runs on the injected clock, which is what keeps a
# staleness case sub-millisecond instead of ninety seconds.
_POLL_SECONDS = 0.01


class FakeSourceAdapter(SourceAdapter):
    def __init__(self, source: Source) -> None:
        self._source = source
        self._items: dict[str, SourceItem] = {}
        self._changed_at: dict[str, AwareDatetime] = {}
        self._states: dict[str, SourceWatchState] = {}
        self._offline = False
        self._credentials_valid = True
        self._closed = False
        self._fail_after: int | None = None
        # The session model. `_server_token` is what the source currently
        # accepts; `_token` is what this adapter last obtained. Expiring a
        # session rotates the former, so the next call sees a mismatch and
        # must re-authenticate -- exactly the shape of the Emby failure.
        self._server_token = "session-0"
        self._token: str | None = None
        self._auth_rejected = False
        self._lock = asyncio.Lock()
        self.authentications = 0
        # One entry per `watch_state` walk, in the order the walks were
        # started: the `start_index` each one was asked for. Recorded rather
        # than inferred from what came back, because a resumed walk and a
        # restarted one over an already-merged prefix produce the *same*
        # stored rows -- the merge is an idempotent upsert -- so the only
        # observable difference between "resumed from page 2,844" and "walked
        # the library again" is the number that was asked for.
        self.resumed_from: list[int] = []
        # The push channel. A ledger of its own shape and the same
        # three-clause health rule, written out below rather than importing
        # `PushHealth` -- for the reason `FakeEmbyServer` defines
        # `_TICKS_PER_SECOND` itself: a fake that shared the
        # implementation's own rule could not disagree with it, and
        # disagreement is the only thing a contract suite is for.
        self._push_queue: asyncio.Queue[SourceEvent] = asyncio.Queue()
        self._push_open = False
        self._push_supported = True
        self._push_messages = 0
        self._push_opened_at: float | None = None
        self._push_last_message_at: float | None = None
        self._push_dropped = False
        self._push_silent = False
        self._push_now = 0.0
        self._push_reconnects = 0
        self.push_stale_after = 90.0

    # -- harness-facing state ------------------------------------------

    def seed(self, item: SourceItem, changed_at: AwareDatetime) -> None:
        self._items[item.external_id] = item
        self._changed_at[item.external_id] = changed_at

    def seed_state(self, state: SourceWatchState) -> None:
        self._states[state.external_id] = state

    def forget(self, external_id: str) -> None:
        self._items.pop(external_id, None)
        self._changed_at.pop(external_id, None)

    def recorded(self, external_id: str) -> tuple[int, bool] | None:
        state = self._states.get(external_id)
        return None if state is None else (state.position_seconds, state.played)

    def go_offline(self) -> None:
        self._offline = True

    def fail_after(self, count: int) -> None:
        self._fail_after = count

    def clear_failure(self) -> None:
        """Undo `fail_after`. `ReconcileService`'s cursor case needs a run
        that failed *followed by* one that succeeds, which is the only way to
        show that a delta walk resumes from the last run that completed
        rather than from the last run that happened."""
        self._fail_after = None

    def reject_credentials(self) -> None:
        self._credentials_valid = False
        self._token = None

    def expire_credentials(self) -> None:
        self._server_token = f"session-{self.authentications + 1}"

    # -- push, for the harness -----------------------------------------

    def push(self, event: SourceEvent) -> None:
        """Queue an event as this source's own channel would deliver it.

        A `WATCH_STATE_CHANGED` naming ids carries **this source's seeded
        states** for them, not whatever the caller happened to put on the
        event -- the same rule `EmbyHarness` follows by rendering the frame
        from the fake server's state. Echoing the caller's own tuple back
        would make `test_events_yields_what_the_source_pushed`'s carried
        assertion agree with the harness by construction, and it would then
        pass against an adapter that fabricated a position.
        """
        if event.kind is SourceEventKind.WATCH_STATE_CHANGED and not event.watch_states:
            seeded = tuple(
                state
                for external_id in event.external_ids
                if (state := self._states.get(external_id)) is not None
            )
            event = SourceEvent(
                kind=event.kind, external_ids=event.external_ids, watch_states=seeded
            )
        self._push_queue.put_nowait(event)

    def silence_push(self) -> None:
        """Deliver nothing more, including whatever is already queued. The
        connection stays open, which is the whole point."""
        self._push_silent = True
        while not self._push_queue.empty():
            self._push_queue.get_nowait()

    def drop_push(self) -> None:
        self._push_dropped = True
        self._push_queue.put_nowait(_DROPPED)

    def advance_push_clock(self, seconds: float) -> None:
        self._push_now += seconds

    def disable_push(self) -> None:
        self._push_supported = False

    # -- the port ------------------------------------------------------

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def push_reconnects(self) -> int:
        """A second, independently written count of the same quantity.

        Overridden rather than inherited from the port's honest `0`, for
        the reason every other clause of this fake's health rule is spelled
        out here: a fake that inherited the default could not disagree with
        an adapter that forgot to override it, and disagreement is the only
        thing a contract suite is for.
        """
        return self._push_reconnects

    @property
    def supports_push(self) -> bool:
        """Grounded in messages, spelled out rather than imported.

        **`self._push_messages > 0` is redundant with the clause after it
        through this fake's own public surface, and is kept anyway** --
        `_drain` writes both fields in the same statement, so nothing can
        reach a state where the count is zero and the instant is not
        `None`. What makes them different is a *reopen*: `_events` clears
        the instant and keeps the count, exactly as `PushHealth.record_open`
        does, so the second open of a channel that has delivered before
        reads `False` on the third clause and not on the second. Same
        equivalent-mutant shape M4 recorded for `jobs.py`'s `GREATEST`
        alongside its `WHERE`, and kept for the same reason: one is the
        lane's history, the other is this connection's.
        """
        return (
            self._push_open
            and self._push_messages > 0
            and self._push_last_message_at is not None
            and self._push_now - self._push_last_message_at <= self.push_stale_after
        )

    async def _ready(self) -> None:
        if self._closed:
            raise PortUnavailable("adapter is closed")
        if self._offline:
            raise PortUnavailable("source is unreachable")
        async with self._lock:
            if self._token is not None and self._token == self._server_token:
                return
            if self._auth_rejected:
                raise PortAuthFailed("credentials were rejected; not retrying yet")
            self.authentications += 1
            if not self._credentials_valid:
                self._auth_rejected = True
                raise PortAuthFailed("credentials were rejected")
            self._token = self._server_token

    async def verify(self) -> SourceStatus:
        if self._closed or self._offline:
            return SourceStatus(reachable=False, authenticated=False, detail="unreachable")
        try:
            await self._ready()
        except PortAuthFailed as exc:
            return SourceStatus(reachable=True, authenticated=False, detail=str(exc))
        return SourceStatus(reachable=True, authenticated=True, server_version="fake-1.0")

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._walk_items(since)

    async def _walk_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        await self._ready()
        yielded = 0
        for external_id, item in list(self._items.items()):
            if since is not None and self._changed_at[external_id] < since:
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                raise PortUnavailable("source went away mid-walk")
            yield item
            yielded += 1

    async def get_item(self, external_id: str) -> SourceItem | None:
        await self._ready()
        return self._items.get(external_id)

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        await self._ready()
        item = self._items.get(external_id)
        if item is None or item.kind is SourceItemKind.SERIES or item.container is None:
            return []
        url = f"{self._source.base_url}/play/{external_id}.{item.container}"
        state = self._states.get(external_id)
        audio_parts = [part for part in (item.audio_codec,) if part]
        layout = _LAYOUTS.get(item.audio_channels or 0)
        if audio_parts and layout:
            audio_parts.append(layout)
        return [
            StreamTarget(
                kind=StreamTargetKind.DIRECT,
                url=url,
                container=item.container,
                video_codec=item.video_codec,
                audio="_".join(audio_parts) or None,
                hdr_format=item.hdr_format,
                resolution=(
                    f"{item.width}x{item.height}"
                    if item.width is not None and item.height is not None
                    else None
                ),
                runtime_seconds=item.runtime_seconds,
                resume_position_seconds=None if state is None else state.position_seconds,
            ),
            StreamTarget(
                kind=StreamTargetKind.DEEP_LINK,
                url=f"infuse://x-callback-url/play?url={quote(url, safe='')}",
                scheme="infuse",
            ),
        ]

    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        # Recorded here rather than in `_walk_states`, so that it is what the
        # **port** was asked for. A subclass that overrides the walk -- and
        # the two in `test_services_watch_sync.py` both do -- would otherwise
        # record whatever it chose to pass down, which for an adapter that
        # re-frames `start_index` is a different number from the one the
        # service asked to resume at.
        self.resumed_from.append(start_index)
        return self._walk_states(since, start_index)

    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        await self._ready()
        yielded = 0
        skipped = 0
        for external_id in list(self._items):
            if since is not None and self._changed_at[external_id] < since:
                continue
            # The skip comes *after* the filter, because `start_index` is an
            # offset into the stream this walk yields rather than into the
            # source's unfiltered set -- which is what a server that filters
            # before it pages hands back, and is exactly what
            # `FakeEmbyServer._list` does (`_ordered` filters, then the slice).
            # Skipping first would make a resumed delta checkpoint a position
            # the real adapter cannot produce.
            if skipped < start_index:
                skipped += 1
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                raise PortUnavailable("source went away mid-walk")
            # An item with no recorded state yields an all-zero state rather
            # than being skipped -- see the contract's
            # test_watch_state_emits_a_zero_state_rather_than_skipping_it.
            yield self._states.get(external_id) or SourceWatchState(
                external_id=external_id, position_seconds=0, played=False
            )
            yielded += 1

    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        """Authoritative, which for a fake means "the same thing the walk
        returns" -- see the module docstring. `None` for an unknown id,
        matching `get_item`, and `_ready()` first so a closed or offline
        adapter raises `PortUnavailable` rather than answering."""
        await self._ready()
        if external_id not in self._items:
            return None
        return self._states.get(external_id) or SourceWatchState(
            external_id=external_id, position_seconds=0, played=False
        )

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        await self._ready()
        # Preserve whatever history is already recorded rather than
        # rebuilding the state from scratch: a real source's write-back does
        # not reset `PlayCount` (verified on Emby -- marking played advances
        # it to 1 idempotently, and a position write leaves it alone), so a
        # fake that zeroed it would make
        # `test_get_watch_state_is_authoritative_about_play_history`
        # order-dependent.
        existing = self._states.get(external_id)
        self._states[external_id] = SourceWatchState(
            external_id=external_id,
            position_seconds=state.position_seconds,
            played=state.played,
            play_count=None if existing is None else existing.play_count,
            last_played_at=None if existing is None else existing.last_played_at,
        )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        if not self._push_supported:
            raise SourceNotSupported("push is disabled on this fake source")
        return self._events()

    @asynccontextmanager
    async def _events(self) -> AsyncIterator[AsyncIterator[SourceEvent]]:
        # On the second and later *open*, guarded on the previous instant
        # rather than incremented unconditionally -- `PushHealth.record_open`
        # states the whole argument, and a fake that started every source's
        # dashboard at 1 would ratify the version that does.
        if self._push_opened_at is not None:
            self._push_reconnects += 1
        self._push_open = True
        self._push_opened_at = self._push_now
        # Cleared, and `_push_messages` deliberately not: the count is the
        # lane's history across reconnects, the instant is evidence about a
        # socket that is now closed. Carrying the instant over would let a
        # fresh connection that delivers nothing inherit its predecessor's
        # freshness -- the exact state this milestone refuses.
        self._push_last_message_at = None
        try:
            yield self._drain()
        finally:
            self._push_open = False

    async def _drain(self) -> AsyncIterator[SourceEvent]:
        while True:
            try:
                event = await asyncio.wait_for(self._push_queue.get(), timeout=_POLL_SECONDS)
            except TimeoutError:
                # A tick, not a failure, and the tick is what runs the
                # watchdog -- the same split `EmbyPushChannel` makes, so
                # that both implementations answer the contract's stalled
                # case for the same structural reason rather than by
                # coincidence.
                if self._push_dropped:
                    raise PortUnavailable("the fake push channel was dropped") from None
                if self._silent_for() > self.push_stale_after:
                    raise PortUnavailable(
                        f"the fake push channel delivered no message in {self._silent_for():.0f}s"
                    ) from None
                continue
            if event is _DROPPED:
                raise PortUnavailable("the fake push channel was dropped")
            if self._push_silent:
                # Queued before the channel went silent. Dropped rather than
                # yielded, and not counted: `silence_push` drains what is
                # already there, so this is the race where a producer got in
                # between the drain and the flag.
                continue
            self._push_messages += 1
            self._push_last_message_at = self._push_now
            yield event

    def _silent_for(self) -> float:
        """Seconds since anything arrived, measured from the open when
        nothing has -- `PushHealth.silent_for`'s rule, re-derived. That
        fallback is what makes a channel that has *never* delivered become
        stale, which is the one failure the watchdog exists for."""
        since = self._push_last_message_at
        if since is None:
            since = self._push_opened_at
        return 0.0 if since is None else self._push_now - since

    async def aclose(self) -> None:
        self._closed = True
        # A closed adapter has no channel, whatever the ledger last saw --
        # `EmbyAdapter.aclose` records the same thing through
        # `PushHealth.record_close`.
        self._push_open = False


class FakeSourceHarness(SourceHarness):
    def __init__(self) -> None:
        self._source = Source(
            id=new_id(),
            kind=SourceKind.EMBY,
            name="Fake Source",
            base_url="https://fake.invalid",
            credentials_ref="ref-fake",
            device_id=str(new_id()),
        )
        self._adapter = FakeSourceAdapter(self._source)

    @property
    def source(self) -> Source:
        return self._source

    @property
    def adapter(self) -> SourceAdapter:
        return self._adapter

    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        self._adapter.seed(item, changed_at)

    async def given_watch_state(self, state: SourceWatchState) -> None:
        self._adapter.seed_state(state)

    async def remove_item(self, external_id: str) -> None:
        self._adapter.forget(external_id)

    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        return self._adapter.recorded(external_id)

    async def go_offline(self) -> None:
        self._adapter.go_offline()

    async def fail_after_items(self, count: int) -> None:
        self._adapter.fail_after(count)

    async def reject_credentials(self) -> None:
        self._adapter.reject_credentials()

    async def expire_credentials(self) -> None:
        self._adapter.expire_credentials()

    def authentications(self) -> int:
        return self._adapter.authentications

    async def push_event(self, event: SourceEvent) -> None:
        self._adapter.push(event)

    async def push_silence(self) -> None:
        self._adapter.silence_push()

    async def push_drop(self) -> None:
        self._adapter.drop_push()

    async def advance_push_clock(self, seconds: float) -> None:
        self._adapter.advance_push_clock(seconds)

    def can_advance_push_clock(self) -> bool:
        return True

    def push_stale_after(self) -> float:
        return self._adapter.push_stale_after

    def can_disable_push(self) -> bool:
        """The only harness that can. `EmbyAdapter` has no state in which
        `events()` raises `SourceNotSupported`, so it declines instead --
        which is why `test_events_raises_source_not_supported_when_push_is_
        unavailable` skips there rather than being deleted."""
        return True

    async def disable_push(self) -> None:
        self._adapter.disable_push()

    async def aclose(self) -> None:
        await self._adapter.aclose()
