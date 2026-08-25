"""`EmbyAdapter` -- the `SourceAdapter` implementation for Emby.

Everything Emby-specific above the wire lives here and in this package's
`mapping`, `playback`, and `session` modules; nothing outside
`usher.adapters.emby` names an Emby field, route, or concept.

**Which routes are verified against the live server.** All of them below,
against Emby **4.9.5.0** on 2026-07-31, driving this class: `GET
/Users/{user}/Items` (listing, delta filters, paging), `GET
/Users/{user}/Items/{item}`, `GET /System/Info`, `GET /System/Info/Public`,
`POST /Users/{user}/Items/{item}/UserData`, `POST
/Users/{user}/PlayedItems/{item}`, and `/Videos/{item}/stream.{container}`.
`POST /Users/AuthenticateByName` is verified separately -- ADR-0004's own
end-to-end session used it -- and was *not* re-exercised here, because the
live run held a token rather than a password. Silent re-authentication on
401 is therefore still unverified end to end against the real server.

`GET /Users/{userId}` (added in M5 for `_is_administrator`) is verified
against that same server and date, but by a throwaway probe rather than by
driving this class -- the method did not exist then. What the probe
established: 200 to the user's *own* non-admin token, carrying a 45-key
`Policy` object with `IsAdministrator` on it. `GET /Users/Me` answers **500**
on this build, so the id is interpolated rather than shortcut.

That run found one route simply wrong: `push_watch_state` reported
positions to `POST /Users/{user}/PlayingItems/{item}/Progress`, which
answers **400** on this server for every body and parameter set tried. See
that method's docstring.

The failure mode of a wrong *query parameter* is benign, and this is now
measured rather than assumed: an invented parameter name was ignored
outright and the request returned the full unfiltered `TotalRecordCount`.
A wrong delta filter therefore degrades to a full walk -- a safe superset,
and exactly what the nightly reconcile does -- never to a silently empty
result.

### Paging

`StartIndex`/`Limit` over `SortBy=DateCreated,SortName&SortOrder=Ascending`.

**Two sort keys, not one.** `StartIndex` paging reads a window out of an
order the server recomputes per request, so it is only safe when that order
is *total*. `DateCreated` alone is not: a bulk library import stamps
thousands of items inside one second, and Emby's own stamp has finite
resolution -- ties are the normal case, not a corner. A server free to
break them differently between two requests reshuffles the window under the
cursor, and items fall through the gap silently, masked one-for-one by
duplicates elsewhere. `SortName` is the tiebreak.

**Emby honours it -- verified 2026-07-31.** Demonstrated on a tie-heavy
primary key rather than hoped for: `SortBy=ProductionYear,SortName` returns
the block of items sharing a year in `SortName` order, and
`SortBy=ProductionYear` alone returns that same block in a different,
insertion-shaped order. The secondary key is applied, so asking for a total
order is a real request and not a no-op.

Tie *instability* was not reproducible on this server -- repeated
`StartIndex=0` pages came back identical, and overlapping `StartIndex=0`/
`StartIndex=5` windows agreed exactly, with and without the tiebreak. So
the second key is a cheap guarantee rather than a demonstrated-necessary
fix here. It stays: it costs one word in a query string, the failure it
prevents is silent, and "this server's query plan happened to be stable
across three requests" is not the same claim as "the order is total".

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
port exists to make impossible. A third bound, `MAX_PAGES`, covers the
opposite failure: neither of the first two fires against a server that
ignores `StartIndex`, and an immortal walk hangs the reconciler rather than
failing it.

### Memory

One page in flight, always. `_walk` yields each payload as it parses it and
never accumulates. Measured against the live deployment on 2026-07-31, a
walk of it is **1,126,674 items** -- 94,438 movies, 32,409 series and
999,827 episodes -- so at the default page size that is one page of JSON
resident rather than a library. The 94,395-movie figure this was built
against was only the movie third of it.

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

# Two different delta filters, because a library edit and a watch-state
# change do not touch the same timestamp. Sending the library one for a
# watch-state walk would miss every item whose only change was being marked
# played -- the entire population that walk exists to find.
#
# Both verified honoured on 2026-07-31, and verified to be genuinely
# different filters rather than aliases: against 1,126,674 items, a cursor
# ten years in the future returned 0 for each, and a 30-day cursor returned
# 28,934 for `MinDateLastSaved` and 29,005 for `MinDateLastSavedForUser`.
LIBRARY_SINCE_PARAM = "MinDateLastSaved"
USER_DATA_SINCE_PARAM = "MinDateLastSavedForUser"

# Two keys, because `StartIndex` paging reads a window out of an order the
# server recomputes for every request and `DateCreated` alone is not a
# total order. Emby applies the second key -- verified 2026-07-31; see the
# Paging section of this module's docstring for how, and for what that run
# could and could not demonstrate.
SORT_BY = "DateCreated,SortName"

# `GET /Users/{userId}` carries the account's `Policy`, which is where
# `IsAdministrator` lives. Verified 2026-07-31: it answers 200 to the user's
# *own* non-admin token, so this needs no elevated rights. `GET /Users/Me`
# answers 500 on that build and is not a usable shortcut.
USER_PATH = "/Users"

# The walk's dead-man's switch. Neither of its two termination conditions
# fires against a server that ignores `StartIndex` -- a reverse proxy
# stripping a parameter it does not know, a build that spells it
# differently: every page comes back full, so there is never an empty one,
# and `start` never passes a `TotalRecordCount` that never arrives.
# Measured against exactly that: 501 requests, 500 items, still going.
#
# Bounded rather than trusted, because an unbounded walk does not fail the
# reconciler, it *hangs* it -- and the same property makes the failure
# untestable, since the test that would catch it never returns either.
#
# 10,000 pages is 2,000,000 items at the default page size. The headroom
# that leaves is **1.8x, not the ~21x this comment used to claim**: a full
# walk of the live deployment is 1,126,674 items (measured 2026-07-31, all
# three item types, not the 94,438 movies alone), i.e. 5,634 pages -- 56% of
# the bound. Still never reached legitimately, and still a bound worth
# having, but it is one library doubling away from mattering rather than
# twenty. Both are constructor knobs, so an operator who lowers `page_size`
# -- or whose library grows past ~2M items -- raises this alongside it.
MAX_PAGES = 10_000


def _segment(value: str) -> str:
    """One path segment, percent-encoded.

    An `external_id` is whatever the source last called an item, and a
    `user_id` is whatever the server said its user was; both are
    interpolated into a request path here. httpx normalises `..` in a path
    exactly the way a browser does, so unquoted this is a path traversal --
    verified: `get_item("../../System/Info")` resolved to `GET
    /Users/System/Info`, and `push_watch_state("../../../Users/U1/Items",
    ...)` aimed *two writes* at an arbitrary endpoint of the caller's
    choosing.

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
        self._clock = clock
        # One ledger for the adapter's whole life, handed to every channel
        # it opens. `reconnects` and `messages_received` are the *lane's*
        # history rather than one connection's, which is what makes
        # `usher.source.push.reconnects` a series worth alerting on and what
        # keeps a lane that has been delivering for hours from reading as
        # one that never has.
        self._health = PushHealth(stale_after=push_stale_after_seconds)
        self._push_connector = push_connect
        self._push_poll_seconds = push_poll_seconds
        self._closed = False

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        """Whether this adapter has a live push channel **right now**, and
        the answer comes from messages.

        `self._health.is_delivering` requires a connection, at least one
        received message, and a recent one. **There is no path from "a
        socket object exists" to `True`** — ADR-0004 measured a handshake
        against a nonexistent path upgrading and being held open, and PRD
        03's reconciler skips a source that says `True` here.
        """
        return self._health.is_delivering(now=self._clock())

    @property
    def push_reconnects(self) -> int:
        """The ledger's own count, which is the lane's history rather than
        this connection's — one `PushHealth` outlives every channel this
        adapter opens."""
        return self._health.reconnects

    @property
    def push_health(self) -> PushHealth:
        """The ledger, for the lane supervisor and for
        `GET /admin/sources/{id}/status`. Read-only by convention; nothing
        outside `adapters/emby` writes it."""
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
                # A log line, not a refusal. PRD 03's "no admin privileges
                # are required" is a permission; nothing enforces it, and
                # ADR-0012 records why refusing here would be worse than
                # saying so (an operator whose only working account is an
                # admin account still needs a catalog).
                logger.warning(
                    "source {source} is configured with an Emby administrator account; "
                    "a captured playback URL or push socket then grants administrator "
                    "access -- configure a normal user (ADR-0012)",
                    source=self._source.name,
                )
            return SourceStatus(
                reachable=True,
                authenticated=True,
                # **`verify()` opens no socket.** A status screen a
                # dashboard polls must not cost a socket per poll against a
                # server PRD 01 measures at 1-5 s per request -- and it
                # would still be answering a question about a socket that is
                # not the one doing the work. This reports the health of the
                # channel *actually running*, if this adapter is the one a
                # push lane is running.
                #
                # `None` ("not probed") rather than `False` ("probed and
                # broken") when no channel has ever opened. The obvious
                # spelling -- the boolean, unconditionally -- turns "nobody
                # has looked" into "push is broken" on every status screen
                # for every source with no lane. ADR-0004: only received
                # messages are evidence, and an upgrade is not; absence of a
                # probe is not evidence either.
                push_available=(
                    None
                    if self._health.opened_at is None
                    else self._health.is_delivering(now=self._clock())
                ),
                is_administrator=is_administrator,
                server_version=_version_of(info) or version,
            )

    async def _is_administrator(self) -> bool | None:
        """`Policy.IsAdministrator` for the authenticated account, or `None`.

        `GET /Users/{userId}` answers 200 to the user's own non-admin token
        and carries a 45-key `Policy` object -- verified 2026-07-31 against
        Emby 4.9.5.0. `GET /Users/Me` answers **500** on that build and is
        not a usable shortcut, so the id is interpolated.

        Never raises. This is one extra request on a status screen, and a
        failure to read a role is not a failure to reach or authenticate
        against a source -- reporting it as one would take
        `GET /admin/sources/{id}/status` from "renders every state a source
        can be in" to "500s on a build that spells this route differently".

        `user_id()` is inside the `try` rather than passed in by the caller,
        which is where the plan put it: it is a `UsherPortError`-raising call
        on the same session, and `verify()` returns rather than raises for
        every expected failure. Unreachable today (the `/System/Info` probe
        above has already authenticated by the time this runs, so the id is
        cached) and one refactor away from not being.

        Three-valued all the way down. A build with no `Policy`, or one whose
        `IsAdministrator` is not a bool, is "not determined" -- `bool(...)`
        of a missing key is `False`, which would render an unperformed check
        as a performed one.
        """
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
        self, *, since_param: str, since: AwareDatetime | None, start_index: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        user_id = await self._session.user_id()
        # The resume point (#41, ADR-0042). `list_items` never passes one --
        # the item lanes restart from their cursor; the watch lane's first
        # walk is the whole library and has to survive a transient failure.
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
        async for payload in self._walk(since_param=LIBRARY_SINCE_PARAM, since=since):
            item = to_source_item(payload)
            if item is not None:
                yield item

    async def _fetch(self, external_id: str, *, op: str = "get_item") -> dict[str, Any] | None:
        """One item's payload, or `None` for a 404.

        `op` is the telemetry label only -- PRD 10 buckets
        `usher.source.request.duration` and the `source.request` span by it.
        It is a parameter because `get_watch_state`'s bounded history
        backfill is thousands of single-item reads, and folding those into
        `get_item`'s bucket makes "how slow is `get_item`" answer a
        different question every night. The route, the 404 handling and the
        `Fields` set are identical for every caller, which is the part that
        must not diverge.
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
            # `redact_path`, not `path`: this is `get_item`'s own raise
            # site rather than `EmbySession.ok`'s, so the session's redaction
            # does not cover it -- and the path holds a user id and an item
            # id (issue #35).
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
            # The URL this builds carries a session token (ADR-0012), so it
            # is never set as a span attribute and never logged.
            return build_stream_targets(
                payload,
                base_url=self._source.base_url,
                access_token=await self._session.access_token(),
            )

    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        """Walk this user's watch state.

        **This walk reports `play_count` and `last_played_at` as `None`,
        always, and that is a property of the server rather than a
        limitation of this code.** Verified 2026-07-31 against Emby 4.9.5.0:
        a *listing* reports `PlayCount: 0` and omits `LastPlayedDate`, even
        for an item whose single-item `GET /Users/{user}/Items/{item}`
        reports `PlayCount: 2` and a real `LastPlayedDate`.
        `position_seconds` and `played` are correct in both. Neither
        `Fields=UserDataPlayState`, `Fields=UserData`, `EnableUserData=true`,
        nor restricting the listing to specific `Ids` changes it.

        Reporting the listing's `0` would write zero over real history at
        every merge. Reporting `None` says exactly what is true -- this read
        cannot determine it -- and `WatchStateRepository.merge_from_source`
        is `COALESCE`-shaped for precisely this value (ADR-0014).
        `get_watch_state` below is the authoritative read, at one request
        per item.

        `start_index` resumes an interrupted walk at that page offset. Sound
        here because this walk is ordered by `DateCreated`, which no edit
        moves, so the prefix already walked does not reorder underneath a
        resumed attempt; a *deletion* shifts it by one and costs the shifted
        item this run, which the merge's idempotent upsert picks up on the
        next one.
        """
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
        """Authoritative watch state for one item, from the single-item
        route that carries the play history the listing route does not.

        Reuses `_fetch`, so a 404 is `None` and every other failure raises,
        exactly as `get_item` behaves -- the two must not diverge or a
        caller learns to tell a deletion from an outage by which method it
        called.
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
        """Write watch state back to Emby: one call, plus a second when the
        item is being marked played.

        Every claim below was verified against the live Emby 4.9.5.0 server
        on 2026-07-31, and the first of them replaced a route that never
        worked at all.

        **The position goes to `POST /Users/{user}/Items/{item}/UserData`,
        as JSON.** The obvious-looking `POST
        /Users/{user}/PlayingItems/{item}/Progress` answers **400 `"Value
        cannot be null. (Parameter 'key')"`** on every body and parameter
        set tried, and so does `POST /Sessions/Playing/Progress`: both are
        *session-scoped playback reporting*, keyed off a play session, and
        Usher never plays anything. The `UserData` route answers 204 and the
        position reads back immediately.

        **`Played` is named in that body even when it is not changing.** The
        route deserialises into a DTO whose unset fields take their C#
        defaults, so a body carrying only `PlaybackPositionTicks` flips a
        played item to unplayed -- observed directly. `PlayCount` and
        `LastPlayedDate` survive the same omission; `Played` does not.

        **Marking played is a second call, and it goes last.** `POST
        /Users/{user}/PlayedItems/{item}` is the only route that advances
        `PlayCount` and stamps `LastPlayedDate`, and it clears the resume
        position as it does so -- so the order is load-bearing exactly as
        PRD 03 says: position first, played last, or a just-finished film
        stays resumable at the last reported second and reappears in
        Continue Watching.

        **Unplaying does *not* use `DELETE /PlayedItems`.** That route is
        destructive well beyond its name: it resets `PlayCount` to 0, clears
        `LastPlayedDate`, *and* clears a non-zero resume position. Reporting
        a position is not a claim that the item was never watched, so the
        unplayed path is the single `UserData` write above, which live Emby
        applies while leaving the household's play history alone.

        A partial failure (position written, played not) raises, exactly as
        the port requires, and PRD 03's caller enqueues a retry. Both writes
        are idempotent -- `POST /PlayedItems` on an already-counted item
        leaves `PlayCount` at 1 rather than incrementing -- so the retry is
        safe.
        """
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
        """One `/embywebsocket` connection.

        **A fresh connection per call**, which is what `PushSupervisor`'s
        reconnect needs, and the *ledger* is shared, which is what makes the
        lane's history survive across them.

        A fresh `EmbyPushChannel` per call as well — but that half is a
        local rather than a guarantee, and saying so is worth more than
        implying otherwise. The plan predicted a cached channel would hand
        back a closed socket and be caught by the supervisor's reconnect
        case; **measured, caching it changes nothing observable**, because
        the channel holds no per-connection state at all — the connection
        lives inside `open()`'s own scope and `open()` connects afresh every
        time. So the load-bearing property is `open()`'s connect, which is
        pinned on the connector's attempt count, and the local here is
        simply the spelling with no dead instance attribute in it.

        Never raises `SourceNotSupported` -- this adapter has a push channel
        -- but `supports_push` still reads `False` until a message arrives
        on it. The port documents that relationship as one-way for exactly
        this reason.
        """
        if self._closed:
            # The port's `aclose` contract: afterwards every method raises
            # `PortUnavailable` rather than whatever the underlying client
            # happens to raise.
            #
            # **Layered, and currently an equivalent mutant -- measured, and
            # kept for the reason `jobs.py` keeps its `GREATEST` alongside
            # its `WHERE`.** `EmbySession._raise_if_closed` is the guard
            # that carries this today: `open()`'s first act is
            # `_socket_url()`, whose first act is `access_token()`, which
            # checks it and raises this same error. So deleting the line
            # below fails nothing. What it buys is the raise happening at
            # the *call* rather than at `__aenter__` -- a supervisor that
            # builds the context manager before entering it learns sooner --
            # and it stops being redundant the moment `events()` does
            # anything before touching the session.
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
        # `EmbyPushChannel.open`'s own `finally` already clears `connected`
        # when the block exits -- this is for the case where it has *not*:
        # a lane parked mid-`async for` when the source is deleted. Without
        # it a status screen reads `push_available: true` for a source that
        # was deleted thirty seconds ago.
        self._health.record_close()
        await self._session.aclose()
        if self._owns_client:
            await self._client.aclose()
