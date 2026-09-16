"""`EmbyAdapter` -- the `SourceAdapter` implementation for Emby."""

import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from opentelemetry import trace
from pydantic import AwareDatetime

from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    emby_datetime,
    to_source_item,
    to_watch_state,
)
from usher.adapters.emby.playback import build_stream_targets
from usher.adapters.emby.push import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    EmbyPushChannel,
    PushConnector,
    PushHealth,
    connect_websocket,
)
from usher.adapters.emby.session import (
    PUBLIC_INFO_PATH,
    SYSTEM_INFO_PATH,
    EmbySession,
    decode_json,
    redact_path,
)
from usher.adapters.http import SourceGate
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceItem,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    WatchStateUpdate,
)

_tracer = trace.get_tracer("usher.source.emby")

# The three types Usher models. A server that ignores this filter returns
# Seasons and BoxSets too; the mapper skips them rather than failing.
ITEM_TYPES = "Movie,Series,Episode"

# Deliberately no `Path`: nothing in M3 or M4 needs a filesystem path, and
# not requesting one keeps it out of `SourceItem.raw`, which PRD 03 stores
# verbatim in `raw_payloads`.
ITEM_FIELDS = (
    "ProviderIds,MediaSources,DateCreated,ProductionYear,RunTimeTicks,"
    "OriginalTitle,ParentIndexNumber,IndexNumber,SeriesId,SeriesName"
)

# Two different delta filters, because a library edit and a watch-state change do not
# touch the same timestamp.
LIBRARY_SINCE_PARAM = "MinDateLastSaved"
USER_DATA_SINCE_PARAM = "MinDateLastSavedForUser"

# Two keys, because `StartIndex` paging reads a window out of an order the
# server recomputes for every request and `DateCreated` alone is not a total
# order. Emby does apply the second key.
SORT_BY = "DateCreated,SortName"

# `GET /Users/{userId}` carries the account's `Policy`, which is where
# `IsAdministrator` lives. It answers 200 to the user's *own* non-admin token,
# so this needs no elevated rights. `GET /Users/Me` answers 500 on some builds
# and is not a usable shortcut.
USER_PATH = "/Users"

# The walk's dead-man's switch.
MAX_PAGES = 10_000


def _segment(value: str) -> str:
    """One path segment, percent-encoded.

    An `external_id` is whatever the source last called an item, and a `user_id`
    is whatever the server said its user was; both are interpolated into a
    request path here. httpx normalises `..` in a path exactly the way a browser
    does, so unquoted this is a path traversal that aims reads *and writes* at an
    endpoint of the caller's choosing.

    httpx's `params=` already neutralises the same trick in a query string.
    Nothing neutralises it in a path; only this does.
    """
    return quote(value, safe="")


def _version_of(body: Mapping[str, Any]) -> str | None:
    version = body.get("Version")
    return version if isinstance(version, str) and version else None


class EmbyAdapter(SourceAdapter):
    def __init__(
        self,
        source: Source,
        credentials: SourceCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        page_size: int = 200,
        max_pages: int = MAX_PAGES,
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
        limiter: SourceGate | None = None,
        push_connect: PushConnector = connect_websocket,
        push_stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        push_poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._page_size = page_size
        self._max_pages = max_pages
        # Ownership is tracked, not assumed: `aclose()` closes a client this
        # adapter created and leaves an injected one alone. Closing someone
        # else's client is what the bulk adapters' no-op `aclose` avoids.
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=source.base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._session = EmbySession(
            self._client,
            credentials,
            source_name=source.name,
            device_id=source.device_id,
            reauth_cooldown_seconds=reauth_cooldown_seconds,
            # Passed through, never built here: the outbound gate is owned by
            # the composition root's `SourceGateRegistry`, so every adapter this
            # deployment opens for one source paces against one gate. `None` --
            # a directly-constructed adapter -- is unthrottled.
            limiter=limiter,
        )
        self._clock = clock
        # One ledger for the adapter's whole life, handed to every channel it opens.
        self._health = PushHealth(stale_after=push_stale_after_seconds)
        self._push_connector = push_connect
        self._push_poll_seconds = push_poll_seconds
        self._closed = False

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        """Whether this adapter has a live push channel **right now**.

        The answer comes from messages: `is_delivering` wants a connection, at
        least one received message, and a recent one. There is no path from "a
        socket object exists" to `True` -- a handshake against a nonexistent path
        upgrades and is held open -- and PRD 03's reconciler skips a source that
        says `True` here.
        """
        return self._health.is_delivering(now=self._clock())

    @property
    def push_reconnects(self) -> int:
        """The lane's history rather than this connection's.

        One `PushHealth` outlives every channel this adapter opens.
        """
        return self._health.reconnects

    @property
    def push_health(self) -> PushHealth:
        """The ledger, for the lane supervisor and for `GET /admin/sources/{id}/status`.

        Read-only by convention; nothing outside `adapters/emby` writes it.
        """
        return self._health

    async def verify(self) -> SourceStatus:
        with _tracer.start_as_current_span("source.verify") as span:
            span.set_attribute("usher.source", self._source.name)
            try:
                public = await self._session.anonymous_json(PUBLIC_INFO_PATH, op="verify_public")
            except PortRateLimited as exc:
                # Rate limited means something answered, so the host is up.
                # Must be caught before UsherPortError -- it is a subclass.
                return SourceStatus(reachable=True, authenticated=False, detail=str(exc))
            except UsherPortError as exc:
                return SourceStatus(reachable=False, authenticated=False, detail=str(exc))
            version = _version_of(public)
            try:
                info = await self._session.json_body("GET", SYSTEM_INFO_PATH, op="verify")
            except UsherPortError as exc:
                return SourceStatus(
                    reachable=True,
                    authenticated=False,
                    server_version=version,
                    detail=str(exc),
                )
            span.set_attribute("usher.authenticated", True)
            is_administrator = await self._is_administrator()
            if is_administrator:
                # A log line, not a refusal. PRD 03's "no admin privileges are
                # required" is a permission, nothing enforces it, and an
                # operator whose only working account is an admin account
                # still needs a catalog.
                logger.warning(
                    "source {source} is configured with an Emby administrator account; "
                    "a captured playback URL or push socket then grants administrator "
                    "access -- configure a normal user",
                    source=self._source.name,
                )
            return SourceStatus(
                reachable=True,
                authenticated=True,
                # **`verify()` opens no socket.** A status screen a dashboard polls must
                # not cost a socket per poll -- and it would still be answering a
                # question about a socket that is not the one doing the work.
                push_available=(
                    None
                    if self._health.opened_at is None
                    else self._health.is_delivering(now=self._clock())
                ),
                is_administrator=is_administrator,
                server_version=_version_of(info) or version,
            )

    async def _is_administrator(self) -> bool | None:
        """`Policy.IsAdministrator` for the authenticated account, or `None`."""
        try:
            user_id = await self._session.user_id()
            body = await self._session.json_body(
                "GET", f"{USER_PATH}/{_segment(user_id)}", op="verify_policy"
            )
        except UsherPortError:
            return None
        policy = body.get("Policy")
        if not isinstance(policy, Mapping):
            return None
        value = policy.get("IsAdministrator")
        return value if isinstance(value, bool) else None

    async def _walk(
        self, *, since_param: str, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[dict[str, Any]]:
        user_id = await self._session.user_id()
        # The resume point (#41). Deliberately no default: every
        # caller states its own, so `list_items` passing 0 is written down
        # rather than inferred from an absent keyword. The item lanes restart
        # from their cursor; the watch lane's first walk is the whole library
        # and has to survive a transient failure.
        start = start_index
        # `for`, not `while True`: the bound is then part of the loop rather
        # than a counter alongside it, and the raise below cannot be reached
        # by any path that should have returned.
        for _ in range(self._max_pages):
            params = {
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES,
                "Fields": ITEM_FIELDS,
                "SortBy": SORT_BY,
                "SortOrder": "Ascending",
                "StartIndex": str(start),
                "Limit": str(self._page_size),
                "EnableTotalRecordCount": "true",
            }
            if since is not None:
                params[since_param] = emby_datetime(since)
            body = await self._session.json_body(
                "GET", f"/Users/{_segment(user_id)}/Items", params=params, op="list"
            )
            items = body.get("Items")
            if not isinstance(items, list):
                # Not a truncation: a caller must be able to tell "the
                # library ended" from "that was not a listing at all".
                raise PortDataMalformed(
                    "Emby's item listing carried no Items array",
                    detail=f"StartIndex={start}",
                )
            if not items:
                return
            for payload in items:
                if isinstance(payload, dict):
                    yield payload
            start += len(items)
            total = body.get("TotalRecordCount")
            # `total > 0`, not `total >= 0`: a server that omits the count
            # (or reports zero while returning items) must not stop the walk
            # at page one.
            if isinstance(total, int) and total > 0 and start >= total:
                return
        raise PortDataMalformed(
            "Emby's item listing never ended; the server appears to ignore StartIndex",
            detail=f"gave up after {self._max_pages} pages at StartIndex={start}",
        )

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._list_items(since)

    async def _list_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        # `start_index=0` always: the item lanes have a working `since`
        # cursor, so a failed walk restarts from it rather than resuming.
        async for payload in self._walk(
            since_param=LIBRARY_SINCE_PARAM, since=since, start_index=0
        ):
            item = to_source_item(payload)
            if item is not None:
                yield item

    async def _fetch(self, external_id: str, *, op: str = "get_item") -> dict[str, Any] | None:
        """One item's payload, or `None` for a 404.

        `op` is the telemetry label only -- PRD 10 buckets
        `usher.source.request.duration` and the `source.request` span by it. It
        is a parameter because `get_watch_state`'s history backfill is thousands
        of single-item reads, and folding those into `get_item`'s bucket makes
        "how slow is `get_item`" answer a different question every night.
        """
        user_id = await self._session.user_id()
        path = f"/Users/{_segment(user_id)}/Items/{_segment(external_id)}"
        # `request`, not `json_body`: a 404 is "gone", which is a value, and
        # every other failure is an error. Conflating them would mark a
        # healthy item unavailable over a flaky network.
        response = await self._session.request("GET", path, params={"Fields": ITEM_FIELDS}, op=op)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            # `redact_path`, not `path`: this is `get_item`'s own raise site
            # rather than `EmbySession.ok`'s, so the session's redaction does
            # not cover it, and the path holds a user id and an item id.
            raise PortUnavailable(f"GET {redact_path(path)} returned HTTP {response.status_code}")
        payload = decode_json(response, path)
        # Some builds answer an unknown id with 200 and an empty object
        # rather than 404. An item with no `Id` is not an item.
        return payload if payload.get("Id") else None

    async def get_item(self, external_id: str) -> SourceItem | None:
        with _tracer.start_as_current_span("source.get_item") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            payload = await self._fetch(external_id)
            span.set_attribute("usher.found", payload is not None)
            return None if payload is None else to_source_item(payload)

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        with _tracer.start_as_current_span("source.stream_targets") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            payload = await self._fetch(external_id)
            if payload is None:
                return []
            # The URL this builds carries a session token, so it is never set
            # as a span attribute and never logged.
            return build_stream_targets(
                payload,
                base_url=self._source.base_url,
                access_token=await self._session.access_token(),
            )

    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        """Walk this user's watch state."""
        return self._watch_state(since, start_index)

    async def _watch_state(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        async for payload in self._walk(
            since_param=USER_DATA_SINCE_PARAM, since=since, start_index=start_index
        ):
            # play_history_is_trustworthy=False: this is the listing route.
            state = to_watch_state(
                payload, source_user_id=user_id, play_history_is_trustworthy=False
            )
            if state is not None:
                yield state

    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        """Authoritative watch state, from the route carrying the play history.

        Reuses `_fetch`, so a 404 is `None` and every other failure raises,
        exactly as `get_item` behaves -- the two must not diverge, or a caller
        learns to tell a deletion from an outage by which method it called.
        """
        with _tracer.start_as_current_span("source.get_watch_state") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            payload = await self._fetch(external_id, op="get_watch_state")
            span.set_attribute("usher.found", payload is not None)
            if payload is None:
                return None
            return to_watch_state(
                payload,
                source_user_id=await self._session.user_id(),
                play_history_is_trustworthy=True,
            )

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to Emby: one call, two when marking played."""
        with _tracer.start_as_current_span("source.push_watch_state") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            span.set_attribute("usher.played", state.played)
            user_id = await self._session.user_id()
            user, item = _segment(user_id), _segment(external_id)
            await self._session.ok(
                "POST",
                f"/Users/{user}/Items/{item}/UserData",
                payload={
                    # Clamped, not trusted: `WatchStateUpdate` is a plain
                    # dataclass with no validation and Emby's tick fields are
                    # unsigned, so a negative would be a 400 on a write-back
                    # PRD 03 then retries forever.
                    "PlaybackPositionTicks": max(state.position_seconds, 0) * TICKS_PER_SECOND,
                    "Played": state.played,
                },
                op="push_progress",
            )
            if state.played:
                await self._session.ok(
                    "POST", f"/Users/{user}/PlayedItems/{item}", op="push_played"
                )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """One `/embywebsocket` connection."""
        if self._closed:
            # The port's `aclose` contract: afterwards every method raises
            # `PortUnavailable` rather than whatever the underlying client happens to
            # raise.
            raise PortUnavailable("this source adapter has been closed")
        channel = EmbyPushChannel(
            self._session,
            base_url=self._source.base_url,
            device_id=self._source.device_id,
            health=self._health,
            connect=self._push_connector,
            clock=self._clock,
            poll_seconds=self._push_poll_seconds,
        )
        return channel.open()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A closed adapter has no channel, whatever the ledger last saw.
        self._health.record_close()
        await self._session.aclose()
        if self._owns_client:
            await self._client.aclose()
