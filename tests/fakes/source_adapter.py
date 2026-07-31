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
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
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

    def reject_credentials(self) -> None:
        self._credentials_valid = False
        self._token = None

    def expire_credentials(self) -> None:
        self._server_token = f"session-{self.authentications + 1}"

    # -- the port ------------------------------------------------------

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        return False

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

    def watch_state(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceWatchState]:
        return self._walk_states(since)

    async def _walk_states(self, since: AwareDatetime | None) -> AsyncIterator[SourceWatchState]:
        await self._ready()
        yielded = 0
        for external_id in list(self._items):
            if since is not None and self._changed_at[external_id] < since:
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

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        await self._ready()
        self._states[external_id] = SourceWatchState(
            external_id=external_id,
            position_seconds=state.position_seconds,
            played=state.played,
        )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        raise SourceNotSupported("this adapter has no push channel")

    async def aclose(self) -> None:
        self._closed = True


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

    async def aclose(self) -> None:
        await self._adapter.aclose()
