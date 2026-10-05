"""`EmbyAdapter` -- the `SourceAdapter` implementation for Emby."""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from itertools import batched
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from opentelemetry import trace
from pydantic import AwareDatetime

from usher.adapters.emby.limit import ListingLimit
from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    emby_datetime,
    to_source_item,
    to_watch_state,
)
from usher.adapters.emby.paging import PAGE_OVERLAP, OffsetWindow
from usher.adapters.emby.planning import SEED_KEY, LibraryUnit, episode_chunks, parse_unit_key
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
    RequestRefused,
    decode_json,
    redact_path,
)
from usher.adapters.http import SourceGate
from usher.domain.source import Source
from usher.domain.sync import WalkStage
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)
from usher.ports.source import (
    DEFAULT_UNIT_KEY,
    SourceAdapter,
    SourceEvent,
    SourceItem,
    SourceItemKind,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    UnitPage,
    WalkPlan,
    WalkUnit,
    WatchStateUpdate,
)

_tracer = trace.get_tracer("usher.source.emby")

# The three types Usher models. A server that ignores this filter returns
# Seasons and BoxSets too; the mapper skips them rather than failing.
ITEM_TYPES = "Movie,Series,Episode"

# Views that only point at items held in libraries. Walking them reads those
# items twice, and counting them can lift the libraries' sum to the source's
# total while an item sits in no library at all.
NOT_LIBRARIES = frozenset({"boxsets", "playlists"})

# Series asked for by `Ids` in one request: a hundred ids keep the URL short.
IDS_PER_REQUEST = 100
NEXT_UP_PATH = "/Shows/NextUp"
SEED_UNIT = WalkUnit(SEED_KEY, WalkStage.SEED, "what the account is watching")

# Deliberately no `Path`: nothing needs a filesystem path, so none is
# requested and none reaches `SourceItem.raw`.
ITEM_FIELDS = (
    "ProviderIds,MediaSources,DateCreated,ProductionYear,RunTimeTicks,"
    "OriginalTitle,ParentIndexNumber,IndexNumber,SeriesId,SeriesName"
)

# Two different delta filters, because a library edit and a watch-state change do not
# touch the same timestamp.
LIBRARY_SINCE_PARAM = "MinDateLastSaved"
USER_DATA_SINCE_PARAM = "MinDateLastSavedForUser"

# A first watch walk asks only for what the account has watched: played items,
# then items holding a resume position. The two overlap.
FIRST_WALK_FILTERS = ("IsPlayed", "IsResumable")

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

# The waits between attempts at one page of a walk: about eight minutes of waits
# across six attempts. A full walk of a large library takes hours and a failed item
# walk restarts from the top, so riding out a server restart on the page it hit is the
# cheap side.
PAGE_RETRY_WAITS = (15.0, 30.0, 60.0, 120.0, 240.0)

# A listing page may read for this long, or for the client's budget if that is longer;
# every other request keeps the client's. A timed-out request does not stop the
# server's query, so asking again early only adds a second copy of the slowest query
# there is.
LISTING_READ_SECONDS = 120.0


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


async def _settle(task: asyncio.Task[Any]) -> None:
    """Cancel a read-ahead the walk no longer wants, and retrieve what it raised.

    Waited on rather than awaited, so its failure is never raised over whatever is
    ending the walk, and the caller's own cancellation still propagates. Retrieved
    by a callback, because asyncio reports an exception nobody read as lost, and
    the caller may be cancelled before the read-ahead finishes.
    """
    task.add_done_callback(_retrieve)
    task.cancel()
    await asyncio.wait({task})


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _version_of(body: Mapping[str, Any]) -> str | None:
    version = body.get("Version")
    return version if isinstance(version, str) and version else None


def _listed_state(payload: dict[str, Any], user_id: str) -> SourceWatchState | None:
    # play_history_is_trustworthy=False: this is the listing route.
    return to_watch_state(payload, source_user_id=user_id, play_history_is_trustworthy=False)


def _listing_query(
    since_param: str, since: AwareDatetime | None, *, filters: str | None = None
) -> dict[str, str]:
    """A listing's parameters, less the paging `_pages` adds to each request."""
    query = {
        "Recursive": "true",
        "IncludeItemTypes": ITEM_TYPES,
        "Fields": ITEM_FIELDS,
        "SortBy": SORT_BY,
        "SortOrder": "Ascending",
    }
    if since is not None:
        query[since_param] = emby_datetime(since)
    if filters is not None:
        query["Filters"] = filters
    return query


class EmbyAdapter(SourceAdapter):
    def __init__(
        self,
        source: Source,
        credentials: SourceCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        page_size: int = 1000,
        max_pages: int = MAX_PAGES,
        unit_max_items: int = 100_000,
        listing_concurrency: int = 4,
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
        limiter: SourceGate | None = None,
        push_connect: PushConnector = connect_websocket,
        push_stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        push_poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._source = source
        self._sleep = sleep
        self._page_size = page_size
        self._max_pages = max_pages
        self._unit_max_items = unit_max_items
        # The library ids, read by the plan or by the first unit a resumed walk asks for.
        self._library_ids: frozenset[str] | None = None
        self._library_lock = asyncio.Lock()
        # Every listing request this adapter sends, each walker's and each
        # read-ahead's, takes its turn from this one limit.
        self._listing_limit = ListingLimit(listing_concurrency, source=source.name)
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
        upgrades and is held open -- and this answer is what the push gauge
        reports and what the stored source's `supports_push` records.
        """
        return self._health.is_delivering(now=self._clock())

    @property
    def push_reconnects(self) -> int:
        """The lane's history rather than this connection's.

        One `PushHealth` outlives every channel this adapter opens.
        """
        return self._health.reconnects

    @property
    def push_messages_received(self) -> int:
        """Every frame counted before it is parsed, so `Sessions` counts as delivery."""
        return self._health.messages_received

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
        self, query: Mapping[str, str], *, start_index: int
    ) -> AsyncGenerator[dict[str, Any]]:
        """Every new entry of one listing, from `start_index` to its end."""
        async with aclosing(self._pages(query, start_index=start_index)) as pages:
            async for entries, _ in pages:
                for entry in entries:
                    yield entry

    async def _pages(
        self,
        query: Mapping[str, str],
        *,
        start_index: int,
        stop: int | None = None,
        path: str | None = None,
    ) -> AsyncGenerator[tuple[list[dict[str, Any]], int]]:
        """Page one listing to its end, one request ahead of the consumer.

        Yields each page's new entries with the `StartIndex` that resumes after it,
        which is the next request's start, reach-back included. The next page is
        asked for as soon as a page arrives and before it is yielded; the request
        outstanding when the consumer stops is cancelled. `stop` bounds the walk,
        and one already at its stop sends nothing. `start_index` is the resume
        point (#41), never defaulted: every caller states its own.
        """
        if path is None:
            path = await self._items_path()
        window = OffsetWindow(limit=self._page_size, start=start_index, stop=stop)
        if window.request_limit == 0:
            return
        pending = self._read(path, query, window.start, window.request_limit, count=True)
        try:
            # `for`, not `while True`: the bound is part of the loop, and the raise
            # below is reachable only by a walk that never ended.
            for number in range(1, self._max_pages + 1):
                body = await pending
                entries = body.get("Items")
                if not isinstance(entries, list):
                    # Not a truncation: a caller must be able to tell "the library
                    # ended" from "that was not a listing at all".
                    raise PortDataMalformed(
                        "Emby's item listing carried no Items array",
                        detail=f"StartIndex={window.start}",
                    )
                page = window.receive(entries, body.get("TotalRecordCount"))
                if page.shifted:
                    logger.warning(
                        "{source}'s listing shifted by at least {overlap} items before "
                        "StartIndex={start}; an item shifted further was not read, and the "
                        "next full walk reads it",
                        source=self._source.name,
                        overlap=window.overlap,
                        start=window.start,
                    )
                resume_at = window.cursor
                if not page.ended and number < self._max_pages:
                    resume_at = window.advance()
                    pending = self._read(path, query, resume_at, window.request_limit, count=False)
                yield page.fresh, resume_at
                if page.ended:
                    return
        finally:
            await _settle(pending)
        raise PortDataMalformed(
            "Emby's item listing never ended; the server appears to ignore StartIndex "
            "or to cap Limit far below the page size",
            detail=f"gave up after {self._max_pages} pages at StartIndex={window.start}",
        )

    def _read(
        self, path: str, query: Mapping[str, str], start: int, limit: int, *, count: bool
    ) -> asyncio.Task[dict[str, Any]]:
        """One page's request, started now and awaited when the walk reaches it."""
        params = {
            **query,
            "StartIndex": str(start),
            "Limit": str(limit),
            "EnableTotalRecordCount": "true" if count else "false",
        }
        return asyncio.create_task(self._page(path, params, start))

    async def _items_path(self) -> str:
        return f"/Users/{_segment(await self._session.user_id())}/Items"

    async def _unit_pages(
        self, query: Mapping[str, str], *, start_index: int, stop: int | None = None
    ) -> AsyncGenerator[UnitPage]:
        """`_pages` as the port's pages; one holding no item Usher models is not yielded."""
        async with aclosing(self._pages(query, start_index=start_index, stop=stop)) as pages:
            async for entries, resume_at in pages:
                items = tuple(item for item in map(to_source_item, entries) if item is not None)
                if items:
                    yield UnitPage(items, resume_at)

    async def _page(
        self, path: str, params: Mapping[str, str], start: int, *, op: str = "list"
    ) -> dict[str, Any]:
        """One page of a walk, asked for again while its failure is one a wait can fix.

        That is an outage -- a 5xx, a 408, a refused or dropped connection, a timeout -- or
        a 429, whose wait is its `Retry-After` when that is longer, up to the longest
        scheduled one. Nothing else is: a refused request and an answer that is not a
        listing would be the same answer next time, a rejected credential needs an
        operator, and a closed adapter -- which raises `PortUnavailable` too -- is shutting
        down.

        Each attempt holds one of the listing limit's slots, and the wait between
        attempts holds none. A failure of the kind asked again drops the limit to
        one; a success counts towards raising it.

        Giving up raises `PortUnavailable` naming the attempts, whatever the last
        failure was. `op` labels the request's span and duration, so a count or a views
        read is not timed as a listing page.
        """
        attempts = len(PAGE_RETRY_WAITS) + 1
        first_failure: float | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._listing_limit.slot():
                    body = await self._session.json_body(
                        "GET", path, params=params, op=op, read_timeout=LISTING_READ_SECONDS
                    )
            except RequestRefused:
                raise
            except (PortUnavailable, PortRateLimited) as exc:
                if self._closed:
                    raise
                self._listing_limit.failed()
                if first_failure is None:
                    first_failure = self._clock()
                if attempt == attempts:
                    elapsed = self._clock() - first_failure
                    raise PortUnavailable(
                        f"{exc} (gave up after {attempt} attempts over {elapsed:.0f}s)"
                    ) from exc
                wait = PAGE_RETRY_WAITS[attempt - 1]
                if isinstance(exc, PortRateLimited) and exc.retry_after is not None:
                    wait = min(max(wait, exc.retry_after), PAGE_RETRY_WAITS[-1])
                logger.warning(
                    "{source}'s listing failed at StartIndex={start} "
                    "(attempt {attempt} of {attempts}): {error}; asking again in {wait:.0f}s",
                    source=self._source.name,
                    start=start,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                    wait=wait,
                )
                await self._sleep(wait)
            else:
                self._listing_limit.succeeded()
                return body

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._list_items(since)

    async def _list_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        # `start_index=0` always: the item lanes have a working `since`
        # cursor, so a failed walk restarts from it rather than resuming.
        query = _listing_query(LIBRARY_SINCE_PARAM, since)
        # `aclosing`, so a consumer that stops closes the walk now, read-ahead
        # included, rather than whenever the generator is collected.
        async with aclosing(self._walk(query, start_index=0)) as payloads:
            async for payload in payloads:
                item = to_source_item(payload)
                if item is not None:
                    yield item

    async def plan_walk(self) -> WalkPlan:
        """The seed, each library's titles, then its episodes in chunks, largest first.

        A library is a view other than a collection or a playlist. Every library,
        the whole source and each watch filter's share of it are counted at once.
        The seed is planned only when both filters narrow the library. When the
        libraries hold fewer items than the source, the seed is followed by one
        walk of everything, and a WARNING names both numbers. A library is counted
        once, over every type a walk lists, so its count rides on its TITLES unit.
        """
        libraries = await self._views()
        self._library_ids = frozenset(view_id for view_id, _ in libraries)
        filtered = len(FIRST_WALK_FILTERS)
        answers = await asyncio.gather(
            self._count(None),
            *(self._count(None, filters=filters) for filters in FIRST_WALK_FILTERS),
            *(self._count(view_id) for view_id, _ in libraries),
            return_exceptions=True,
        )
        counts: list[int] = []
        for answer in answers:
            # Raised only once every count has settled, so none is left running with
            # nobody to read what it raised.
            if isinstance(answer, BaseException):
                raise answer
            counts.append(answer)
        total, watched, held = counts[0], counts[1 : 1 + filtered], counts[1 + filtered :]
        # The seed is what each watch filter narrows the library to. A server that
        # ignored one, or a library watched end to end, would make it a
        # whole-library walk ahead of the real one.
        seed = (SEED_UNIT,) if all(count < total for count in watched) else ()
        if not libraries or sum(held) < total:
            logger.warning(
                "{source}'s libraries hold {held} items against a total of {total}; "
                "walking the whole library as one unit",
                source=self._source.name,
                held=sum(held),
                total=total,
            )
            whole = WalkUnit(DEFAULT_UNIT_KEY, WalkStage.TITLES, "the whole library", total)
            return WalkPlan((*seed, whole), expected_total=total)
        ranked = sorted(zip(libraries, held, strict=True), key=lambda pair: pair[1], reverse=True)
        units: list[WalkUnit] = list(seed)
        for (view_id, name), count in ranked:
            titles = LibraryUnit(WalkStage.TITLES, view_id)
            units.append(WalkUnit(titles.key, WalkStage.TITLES, titles.label(name), count))
        for (view_id, name), count in ranked:
            for chunk in episode_chunks(view_id, count, self._unit_max_items):
                units.append(WalkUnit(chunk.key, WalkStage.EPISODES, chunk.label(name)))
        return WalkPlan(tuple(units), expected_total=total)

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        if key == SEED_KEY:
            # Every seed page resumes at 0, so `start_index` is always 0 here or
            # stale; see `_seed`.
            return self._seed()
        if key == DEFAULT_UNIT_KEY:
            query = _listing_query(LIBRARY_SINCE_PARAM, None)
            return self._unit_pages(query, start_index=start_index)
        unit = parse_unit_key(key)
        if unit is None:
            raise PortDataMalformed(f"no plan of this adapter's could name walk unit {key!r}")
        return self._library_unit(unit, start_index)

    async def _library_unit(self, unit: LibraryUnit, start_index: int) -> AsyncGenerator[UnitPage]:
        # A library gone before this adapter first read the views has nothing left to
        # walk, and its id is not sent: a server can answer a `ParentId` it does not
        # know with the whole library. A unit never reads the views again, so one
        # removed after that read is still asked for.
        if unit.view_id not in await self._known_libraries():
            logger.warning(
                "{source} no longer lists the library behind walk unit {key!r}; "
                "ending the unit empty",
                source=self._source.name,
                key=unit.key,
            )
            return
        query = {
            **_listing_query(LIBRARY_SINCE_PARAM, None),
            "ParentId": unit.view_id,
            "IncludeItemTypes": unit.item_types,
        }
        # A bounded chunk reads `PAGE_OVERLAP` items past its end, so a shift at the
        # boundary with the next chunk is covered the way one between pages is.
        stop = None if unit.upper is None else unit.upper + PAGE_OVERLAP
        pages = self._unit_pages(query, start_index=max(start_index, unit.lower), stop=stop)
        async with aclosing(pages) as unit_pages:
            async for page in unit_pages:
                yield page

    async def _seed(self) -> AsyncGenerator[UnitPage]:
        """What the account is watching: played, then in progress, then up next.

        Each page leads with its series: first those its episodes need that neither
        it nor an earlier page holds, fetched by `Ids`, then its own, in the server's
        order. So every episode comes after its series, save one whose series `Ids`
        did not return, which `_series_for` warns of. Only one page is ever held.
        Every page resumes at 0, so a resumed seed starts again: a count into
        listings that may have changed since could skip an item the watch lane is
        about to look for.
        """
        user_id = await self._session.user_id()
        listings: list[tuple[str | None, dict[str, str]]] = [
            (None, _listing_query(LIBRARY_SINCE_PARAM, None, filters=filters))
            for filters in FIRST_WALK_FILTERS
        ]
        listings.append((NEXT_UP_PATH, {"UserId": user_id, "Fields": ITEM_FIELDS}))
        yielded: set[str] = set()
        for path, query in listings:
            async with aclosing(self._pages(query, start_index=0, path=path)) as pages:
                async for entries, _ in pages:
                    fresh = [
                        item
                        for item in map(to_source_item, entries)
                        if item is not None and item.external_id not in yielded
                    ]
                    series = await self._series_for(fresh, yielded)
                    own = [item for item in fresh if item.kind is SourceItemKind.SERIES]
                    rest = [item for item in fresh if item.kind is not SourceItemKind.SERIES]
                    page = (*series, *own, *rest)
                    if page:
                        yielded.update(item.external_id for item in page)
                        yield UnitPage(page, resume_at=0)

    async def _series_for(self, items: Sequence[SourceItem], yielded: set[str]) -> list[SourceItem]:
        """The series of `items`' episodes that neither they nor an earlier page hold.

        Only a series asked for is kept, whatever else comes back, and one WARNING
        counts those asked for that did not come back.
        """
        held = yielded | {item.external_id for item in items}
        missing = sorted(
            {item.series_external_id for item in items if item.series_external_id} - held
        )
        series: list[SourceItem] = []
        absent = 0
        for chunk in batched(missing, IDS_PER_REQUEST, strict=False):
            # `Limit` is the chunk's length, so a server that ignores `Ids` sends no
            # more items than were asked for.
            params = {"Ids": ",".join(chunk), "Fields": ITEM_FIELDS, "Limit": str(len(chunk))}
            body = await self._page(await self._items_path(), params, 0)
            entries = body.get("Items")
            if not isinstance(entries, list):
                raise PortDataMalformed("Emby's series listing carried no Items array")
            # Each series asked for is kept once; `asked` ends holding those that did not
            # come back.
            asked = set(chunk)
            for item in map(to_source_item, entries):
                if item is not None and item.external_id in asked:
                    asked.discard(item.external_id)
                    series.append(item)
            absent += len(asked)
        if absent:
            logger.warning(
                "{source} did not return {absent} of the {count} series the seed asked for "
                "by Ids; their episodes are seeded without them",
                source=self._source.name,
                absent=absent,
                count=len(missing),
            )
        return series

    async def _views(self) -> list[tuple[str, str]]:
        """The account's libraries, as `(id, name)`."""
        user_id = await self._session.user_id()
        body = await self._page(f"/Users/{_segment(user_id)}/Views", {}, 0, op="views")
        entries = body.get("Items")
        if not isinstance(entries, list):
            raise PortDataMalformed("Emby's view listing carried no Items array")
        libraries: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("CollectionType") in NOT_LIBRARIES:
                continue
            view_id, name = entry.get("Id"), entry.get("Name")
            if isinstance(view_id, str) and view_id:
                libraries.append((view_id, name if isinstance(name, str) and name else view_id))
        return libraries

    async def _known_libraries(self) -> frozenset[str]:
        """The library ids, read once per adapter however many walkers ask at once."""
        async with self._library_lock:
            if self._library_ids is None:
                self._library_ids = frozenset(view_id for view_id, _ in await self._views())
            return self._library_ids

    async def _count(self, view_id: str | None, *, filters: str | None = None) -> int:
        """How many items a walk lists: in one library, the whole source, or its filtered share."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Limit": "0",
            "EnableTotalRecordCount": "true",
        }
        if view_id is not None:
            params["ParentId"] = view_id
        if filters is not None:
            params["Filters"] = filters
        body = await self._page(await self._items_path(), params, 0, op="count")
        total = body.get("TotalRecordCount")
        # A count the server left out is not a count of nothing: a source total
        # read as zero would pass every coverage check.
        if not isinstance(total, int) or total < 0:
            raise PortDataMalformed("Emby's count carried no TotalRecordCount")
        return total

    async def _fetch(self, external_id: str, *, op: str = "get_item") -> dict[str, Any] | None:
        """One item's payload, or `None` for a 404.

        `op` is the telemetry label only -- `usher.source.request.duration`
        (PRD 10) and the session's `source.request` span are bucketed by it. It
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
    ) -> AsyncGenerator[SourceWatchState]:
        """A delta since `since`; with none, the account's played and in-progress items."""
        return self._watch_state(since, start_index)

    async def _watch_state(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncGenerator[SourceWatchState]:
        user_id = await self._session.user_id()
        if since is not None:
            query = _listing_query(USER_DATA_SINCE_PARAM, since)
            async with aclosing(self._walk(query, start_index=start_index)) as payloads:
                async for payload in payloads:
                    state = _listed_state(payload, user_id)
                    if state is not None:
                        yield state
            return
        # A resumed first walk starts its first listing at `start_index` and its
        # second at 0. `WatchStateSyncService` never resumes one -- it starts
        # again -- so this only keeps the port's promise.
        yielded: set[str] = set()
        for number, filters in enumerate(FIRST_WALK_FILTERS):
            query = _listing_query(USER_DATA_SINCE_PARAM, None, filters=filters)
            unwatched = watched = 0
            walk = self._walk(query, start_index=start_index if number == 0 else 0)
            async with aclosing(walk) as payloads:
                async for payload in payloads:
                    state = _listed_state(payload, user_id)
                    if state is None or state.external_id in yielded:
                        continue
                    if not state.played and state.position_seconds <= 0:
                        unwatched += 1
                        continue
                    yielded.add(state.external_id)
                    watched += 1
                    yield state
            if number == 0 and unwatched > watched:
                # Unwatched entries strictly outnumbering watched ones mean the server
                # ignored `Filters` and listed everything, resume positions included, so
                # a second walk would only repeat it. A tie or an empty listing goes on:
                # a Series marked played that has since gained an episode may match
                # `IsPlayed` and still read unwatched.
                logger.warning(
                    "{source} appears to ignore Filters: its first watch walk's played "
                    "listing was mostly unwatched ({unwatched} unwatched skipped, {watched} "
                    "watched yielded), so the walk did not ask for the in-progress listing",
                    source=self._source.name,
                    unwatched=unwatched,
                    watched=watched,
                )
                return

    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        """Authoritative watch state, from the route carrying the play history.

        Reuses `_fetch`, so a 404 is `None` and every other failure raises,
        exactly as `get_item` behaves -- the two must not diverge, or a caller
        learns to tell a deletion from an outage by which method it called. A
        position too large to store is `None` too, logged by `to_watch_state`.
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
