"""Binds a real `EmbyAdapter` to `FakeEmbyServer` for the contract suite.

Page size two, deliberately: the contract seeds seven items for its paging
cases, so the walk crosses four page boundaries rather than trivially
fitting in one.

The `httpx.AsyncClient` is injected, so `EmbyAdapter.aclose()` leaves it
open -- the contract closes the adapter itself in two of its cases, and this
harness's own `aclose()` is what finally disposes of the client.

**The transport really awaits, and that is not a detail.** This runs on
`tests/fakes/slow_transport.py` rather than the bare `httpx.MockTransport`
the fake server hands out, so `observed_overlap` below can return a real
number and `test_operations_recover_from_an_expired_credential` can mean
what it looks like it means. Measured directly: over `MockTransport`, four
`asyncio.gather`-ed calls against an expired session produce exactly one
authentication *even with both of `EmbySession`'s locks deleted and the
generation short-circuit removed*, because nothing in that transport ever
awaits on the way to its handler -- so the event loop runs one gathered call
all the way through its own re-auth before starting the next, and the other
three read an already-fresh token without racing for it. The contract's
`<= 1` assertion never discriminates there. Over this transport it does.

The cost is ~20 ms per upstream request, which is why it is worth saying
what it buys: without it this whole run would inherit a vacuous
single-flight claim from a case that reads like one and is not.
"""

import httpx
from pydantic import AwareDatetime, SecretStr

from tests.contract.source_harness import SourceHarness
from tests.fakes.emby_server import FakeEmbyServer
from tests.fakes.push_connection import FakePushConnection, FakePushConnector
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceWatchState,
)

PAGE_SIZE = 2


class EmbyHarness(SourceHarness):
    def __init__(self) -> None:
        self._server = FakeEmbyServer(page_size=PAGE_SIZE)
        self._source = Source(
            id=new_id(),
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-emby",
            device_id=str(new_id()),
        )
        self._transport = SlowTransport(self._server.handle)
        self._client = httpx.AsyncClient(transport=self._transport, base_url=self._source.base_url)
        # A fake connector, because from M5 `events()` really opens
        # something. Without it the contract's push case resolves
        # `emby.invalid` for real -- measured, it reached DNS and came back
        # `gaierror` -- which is both a network call the suite forbids and a
        # `PortUnavailable` where the case expected either a channel or
        # `SourceNotSupported`. A connection is queued rather than left to
        # the connector's own mint-on-demand so that `push_event` works
        # before the channel has been opened; `_live_push` below prefers
        # whatever was handed out most recently, so a reconnect is followed
        # rather than arranged against a dead object.
        self._push = FakePushConnection()
        self._push_connector = FakePushConnector([self._push])
        # Frozen at zero and moved only by `advance_push_clock`. This is the
        # adapter's *push* clock and nothing else reads it: `PushHealth`'s
        # three instants are the only consumers, so freezing it costs the
        # rest of the contract nothing and buys a ninety-second staleness
        # window in under a millisecond, twice per run.
        self._push_now = 0.0
        self._adapter = EmbyAdapter(
            self._source,
            SourceCredentials(
                username=self._server.username, password=SecretStr(self._server.password)
            ),
            client=self._client,
            page_size=PAGE_SIZE,
            push_connect=self._push_connector,
            push_poll_seconds=0.001,
            # A closure over the attribute, not a captured value, so
            # `advance_push_clock` really moves the adapter's own clock --
            # the trick `EmbySession`'s injected clock already allows, one
            # object over.
            clock=lambda: self._push_now,
        )

    @property
    def source(self) -> Source:
        return self._source

    @property
    def adapter(self) -> SourceAdapter:
        return self._adapter

    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        """Render `item` into Emby's JSON, held as changed at `changed_at`.

        Takes **both** widenings `SourceHarness.given_item` permits, and
        says so here rather than leaving them to be discovered:

        - `changed_at` is compared at whole-second resolution, because
          Emby's `MinDateLastSaved` filter is a whole-second stamp. A
          `since` window may therefore return a superset; the port already
          permits that and callers deduplicate by `external_id`.
        - an item with no `container` is a folder to Emby, and a folder has
          no `MediaSources` entry to hang a codec, a file size, a channel
          count, or an HDR format off -- those read back `None`. Width and
          height survive, because Emby carries them at item level too. See
          `tests/fakes/emby_server.py`'s own docstring for the full
          reasoning; nothing seeds such an item.
        """
        self._server.add_item(item, changed_at)

    async def given_watch_state(self, state: SourceWatchState) -> None:
        self._server.set_watch_state(state)

    async def remove_item(self, external_id: str) -> None:
        self._server.remove_item(external_id)

    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        return self._server.recorded_watch_state(external_id)

    async def go_offline(self) -> None:
        self._server.offline = True

    async def fail_after_items(self, count: int) -> None:
        self._server.fail_after = count

    async def reject_credentials(self) -> None:
        self._server.reject_credentials()

    async def expire_credentials(self) -> None:
        self._server.expire_session()

    def authentications(self) -> int:
        return self._server.authentications

    def observed_overlap(self) -> int | None:
        """The most requests this harness ever saw in flight at once.

        A real number rather than the ABC's `None`, which is what upgrades
        the contract's expired-credential case from "recovery happened" to
        "recovery happened under genuine concurrency, once". Every request
        in this harness goes through `SlowTransport`, so the four gathered
        calls in that case really are simultaneous -- and if they ever stop
        being, the contract's own `>= 2` assertion says so instead of
        quietly certifying a sequential run as single flight.
        """
        return self._transport.max_in_flight

    # -- push --------------------------------------------------------------

    @property
    def _live_push(self) -> FakePushConnection:
        """The connection the adapter is actually holding.

        `handed_out[-1]` rather than the queued one, because `events()`
        connects afresh on every call and a harness that kept arranging
        against the first object would silently stop affecting the channel
        the moment anything reconnected -- a `push_drop` that dropped
        nothing, which reads as a passing case.

        **A known equivalent mutant against the contract suite as it stands,
        and kept anyway**, the way `jobs.py` keeps its `GREATEST` alongside
        its `WHERE`. Measured: collapsing this to `return self._push` leaves
        all 49 cases green on both subclasses, because no case opens
        `events()` twice, so the queued connection *is* the one handed out.
        What it buys is the first reconnect case (`services/push.py`) not
        having to discover this, and it costs one indexing expression.
        """
        return (
            self._push_connector.handed_out[-1] if self._push_connector.handed_out else self._push
        )

    async def push_event(self, event: SourceEvent) -> None:
        """Render a `SourceEvent` into the message Emby would have sent.

        The translation ADR-0013 exists for: the contract speaks
        `SourceEvent` and this turns it into a wire frame, so the same
        assertions run against a second source by writing a second harness
        rather than a second suite.

        A `WATCH_STATE_CHANGED` renders `UserDataChanged` **from the
        server's current state for those ids**, not from the event's own
        `watch_states`, so a case that seeded a state through
        `given_watch_state` and then pushed sees the seeded values -- and an
        adapter that fabricated them instead of parsing the frame would
        fail rather than agree with the harness by construction.
        """
        if event.kind is SourceEventKind.WATCH_STATE_CHANGED:
            frame = self._server.user_data_changed_frame(event.external_ids)
        elif event.kind is SourceEventKind.ITEM_ADDED:
            frame = self._server.library_changed_frame(added=event.external_ids)
        elif event.kind is SourceEventKind.ITEM_UPDATED:
            frame = self._server.library_changed_frame(updated=event.external_ids)
        else:
            frame = self._server.library_changed_frame(removed=event.external_ids)
        self._live_push.deliver(frame)

    async def push_silence(self) -> None:
        self._live_push.stall()

    async def push_drop(self) -> None:
        self._live_push.drop("connection closed by peer")

    async def advance_push_clock(self, seconds: float) -> None:
        self._push_now += seconds

    def can_advance_push_clock(self) -> bool:
        return True

    def push_stale_after(self) -> float:
        return self._adapter.push_health.stale_after

    # `can_disable_push` is deliberately left at the base's `False`.
    # `EmbyAdapter` has no state in which `events()` raises
    # `SourceNotSupported` -- it always has a channel to offer and finds out
    # afterwards whether it delivers -- so implementing it would mean
    # inventing one, and the contract case skips instead.

    async def aclose(self) -> None:
        await self._adapter.aclose()
        await self._client.aclose()
