"""`EmbyAdapter` -- the `SourceAdapter` implementation for Emby.

Everything Emby-specific above the wire lives here and in this package's
`mapping`, `playback`, and `session` modules; nothing outside
`usher.adapters.emby` names an Emby field, route, or concept.

**Which routes are verified against the live server.** `POST
/Users/AuthenticateByName` is -- ADR-0004's own end-to-end session used it,
and it used the played/unplayed toggle below too. The rest are the standard
Emby 4.9 routes and have not been exercised from this code; M3's definition
of done requires a live run before the milestone closes. The failure mode
of a wrong *query parameter* is deliberately benign: Emby ignores
parameters it does not know, so a wrong delta filter degrades to a full
walk -- a safe superset, and exactly what the nightly reconcile does -- and
never to a silently empty result.

### Paging

`StartIndex`/`Limit` over `SortBy=DateCreated,SortName&SortOrder=Ascending`.

**Two sort keys, not one.** `StartIndex` paging reads a window out of an
order the server recomputes per request, so it is only safe when that order
is *total*. `DateCreated` alone is not: a bulk library import stamps
thousands of items inside one second, and Emby's own stamp has finite
resolution -- ties are the normal case, not a corner. A server free to
break them differently between two requests reshuffles the window under the
cursor, and items fall through the gap silently, masked one-for-one by
duplicates elsewhere. `SortName` is the tiebreak. It is not a guaranteed
total order either (two versions of one film can share both keys), but it
collapses the realistic case to nothing, and it is one of the sort fields
Emby's `/Items` documentation lists, so an unknown-field degradation is not
in play. **Not yet exercised against the live server** -- see the route
note above; M3's live run is where this is confirmed.

**Ascending, for a narrower reason than it looks.** Any insertion mid-walk
shifts everything to its right, which produces *duplicates*, and the port
permits those. Ascending by `DateCreated` puts a newly added item past the
window entirely, so a mid-walk insertion costs nothing at all, where
descending puts it at index 0 and makes every later page re-serve an item
already read. Skips come from the other two directions: a *deletion* shifts
unread items left past the cursor -- a bounded imprecision the nightly full
reconcile covers -- and tie instability, which is what the second sort key
above is for.

The walk terminates on an empty page, and also when `TotalRecordCount` says
everything has been read. The second condition is guarded on a *positive*
count, because a server that omits or zeroes the count would otherwise stop
the walk at page one -- a silent truncation, which is the one failure this
port exists to make impossible.

### Memory

One page in flight, always. `_walk` yields each payload as it parses it and
never accumulates. The deployment this was built for holds 94,395 movies
across 17 libraries; at the default page size that is one page of JSON
resident, not a library.

### Spans

`get_item`, `stream_targets`, `push_watch_state`, and `verify` each open
one. `list_items` and `watch_state` deliberately do not:
`start_as_current_span` sets a context variable, and a `with` block that
spans a `yield` leaks that context to whoever resumes the generator and
holds the span open for as long as a caller keeps a half-consumed iterator.
`EmbySession` already opens a span per HTTP request, which is the
granularity `usher.source.request.duration` is bucketed by anyway.

No span attribute here is ever a URL. A direct-play URL carries the
source's session token (ADR-0012), and a span attribute is one of the four
places that token must never appear.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import AwareDatetime

from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    emby_datetime,
    to_source_item,
    to_watch_state,
)
from usher.adapters.emby.playback import build_stream_targets
from usher.adapters.emby.session import (
    PUBLIC_INFO_PATH,
    SYSTEM_INFO_PATH,
    EmbySession,
    decode_json,
)
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
    SourceNotSupported,
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

# Two different delta filters, because a library edit and a watch-state
# change do not touch the same timestamp. Sending the library one for a
# watch-state walk would miss every item whose only change was being marked
# played -- the entire population that walk exists to find.
LIBRARY_SINCE_PARAM = "MinDateLastSaved"
USER_DATA_SINCE_PARAM = "MinDateLastSavedForUser"

# Two keys, because `StartIndex` paging reads a window out of an order the
# server recomputes for every request and `DateCreated` alone is not a
# total order -- see the Paging section of this module's docstring.
SORT_BY = "DateCreated,SortName"


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
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
    ) -> None:
        self._source = source
        self._page_size = page_size
        # Ownership is tracked, not assumed: `aclose()` closes a client this
        # adapter created and leaves an injected one alone. Closing someone
        # else's client is the mistake the bulk adapters' no-op `aclose`
        # exists to avoid, arrived at from the other direction.
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
        )
        self._closed = False

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        """`False` until M5 builds the WebSocket listener.

        Not a placeholder: PRD 03 specifies exactly this as the fallback for
        a source whose socket cannot be established, and the reconciler's
        nightly walk covers it. Push itself is verified working (ADR-0004);
        it is sequenced, not blocked.
        """
        return False

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
            return SourceStatus(
                reachable=True,
                authenticated=True,
                # `None`, never `True`. ADR-0004: a WebSocket handshake
                # against a *nonexistent* path also upgrades and also
                # receives `Sessions`, so an upgrade is not evidence of
                # anything. Only received messages are, and M5 builds the
                # probe that asserts on them.
                push_available=None,
                server_version=_version_of(info) or version,
            )

    async def _walk(
        self, *, since_param: str, since: AwareDatetime | None
    ) -> AsyncIterator[dict[str, Any]]:
        user_id = await self._session.user_id()
        start = 0
        while True:
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
                "GET", f"/Users/{user_id}/Items", params=params, op="list"
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

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._list_items(since)

    async def _list_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        async for payload in self._walk(since_param=LIBRARY_SINCE_PARAM, since=since):
            item = to_source_item(payload)
            if item is not None:
                yield item

    async def _fetch(self, external_id: str) -> dict[str, Any] | None:
        user_id = await self._session.user_id()
        path = f"/Users/{user_id}/Items/{external_id}"
        # `request`, not `json_body`: a 404 is "gone", which is a value, and
        # every other failure is an error. Conflating them would mark a
        # healthy item unavailable over a flaky network.
        response = await self._session.request(
            "GET", path, params={"Fields": ITEM_FIELDS}, op="get_item"
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PortUnavailable(f"GET {path} returned HTTP {response.status_code}")
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
            # The URL this builds carries a session token (ADR-0012), so it
            # is never set as a span attribute and never logged.
            return build_stream_targets(
                payload,
                base_url=self._source.base_url,
                access_token=await self._session.access_token(),
                device_id=self._source.device_id,
            )

    def watch_state(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceWatchState]:
        return self._watch_state(since)

    async def _watch_state(self, since: AwareDatetime | None) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        async for payload in self._walk(since_param=USER_DATA_SINCE_PARAM, since=since):
            state = to_watch_state(payload, source_user_id=user_id)
            if state is not None:
                yield state

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to Emby, in two calls.

        Emby has no endpoint that sets position and played together, so this
        is not atomic -- and the order is load-bearing. **Position first,
        played flag last:** marking an item played clears its resume
        position server-side, so writing the position afterwards leaves a
        just-finished film resumable at whatever second the client last
        reported, which is how it reappears in Continue Watching.

        A partial failure (position written, played not) raises, exactly as
        the port requires, and PRD 03's caller enqueues a retry. Both writes
        are idempotent, so the retry is safe.
        """
        with _tracer.start_as_current_span("source.push_watch_state") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            span.set_attribute("usher.played", state.played)
            user_id = await self._session.user_id()
            await self._session.ok(
                "POST",
                f"/Users/{user_id}/PlayingItems/{external_id}/Progress",
                params={
                    # Clamped, not trusted: `WatchStateUpdate` is a plain
                    # dataclass with no validation and Emby's PositionTicks
                    # is unsigned, so a negative would be a 400 on a
                    # write-back PRD 03 then retries forever.
                    "PositionTicks": str(max(state.position_seconds, 0) * TICKS_PER_SECOND)
                },
                op="push_progress",
            )
            await self._session.ok(
                "POST" if state.played else "DELETE",
                f"/Users/{user_id}/PlayedItems/{external_id}",
                op="push_played",
            )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        raise SourceNotSupported(
            "the Emby push channel lands in M5; until then this source is covered by the reconciler"
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._session.aclose()
        if self._owns_client:
            await self._client.aclose()
