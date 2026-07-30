"""EmbyAdapter behaviours the source-agnostic contract cannot express.

The contract suite (run against this adapter in the next task) pins what
every `SourceAdapter` must do. This module pins what *Emby's* adapter must
do: which query parameters the walk sends, how it terminates, which
endpoints a write-back uses and in which order, and how `verify` tells
"unreachable" from "bad credentials".
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from tests.fakes.emby_server import SERVER_VERSION, USER_ID, FakeEmbyServer
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable
from usher.ports.source import (
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceWatchState,
    WatchStateUpdate,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SOURCE = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id="9d1f0b6c-0000-7000-8000-000000000001",
)


def _movie(index: int) -> SourceItem:
    return SourceItem(
        external_id=f"movie-{index}",
        name=f"Movie {index}",
        kind=SourceItemKind.MOVIE,
        year=2000 + index,
        provider_ids={"imdb": f"tt000000{index}"},
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=T0,
    )


def _adapter(server: FakeEmbyServer, *, page_size: int = 2) -> EmbyAdapter:
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url),
        page_size=page_size,
    )


def _on(handler: Callable[[httpx.Request], httpx.Response]) -> EmbyAdapter:
    """An adapter over a hand-written handler, for the shapes `FakeEmbyServer`
    is deliberately too well-behaved to produce."""
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SOURCE.base_url),
    )


def _authenticated(request: httpx.Request) -> httpx.Response | None:
    """The authentication leg every hand-written handler below shares."""
    if request.url.path == "/Users/AuthenticateByName":
        return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": USER_ID}})
    return None


# --- the walk --------------------------------------------------------


async def test_the_walk_pages_until_the_library_is_exhausted() -> None:
    server = FakeEmbyServer(page_size=2)
    for index in range(5):
        server.add_item(_movie(index), T0)
    adapter = _adapter(server, page_size=2)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sorted(seen) == [f"movie-{index}" for index in range(5)]
    listings = [entry for entry in server.requests if entry.endswith("/Items")]
    # 5 items over pages of 2 is three requests: TotalRecordCount stops the
    # walk after the third rather than paying a fourth for an empty page.
    assert len(listings) == 3


async def test_the_walk_asks_for_the_types_and_fields_the_mapper_needs() -> None:
    """A missing `Fields=MediaSources` is the failure mode worth pinning:
    every item comes back with no container, no codec and no HDR, and
    nothing raises -- the catalog just quietly has no quality facts.

    `Recursive` is pinned for the same reason from the other direction:
    without it Emby answers with the *top level* of each library -- folders,
    not films -- and again nothing raises.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    captured: list[httpx.Request] = []
    original = server.handle

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return original(request)

    adapter = _on(spy)
    try:
        # Drained rather than `anext`-ed: a half-consumed async generator
        # would be closed by the garbage collector at an arbitrary later
        # point, in a different test's event loop.
        items = [entry async for entry in adapter.list_items()]
    finally:
        await adapter.aclose()
    item = items[0]
    listing = next(r for r in captured if r.url.path.endswith("/Items"))
    fields = listing.url.params["Fields"]
    assert "MediaSources" in fields
    assert "ProviderIds" in fields
    assert "Path" not in fields
    assert listing.url.params["IncludeItemTypes"] == "Movie,Series,Episode"
    assert listing.url.params["Recursive"] == "true"
    assert item.container == "mkv"


async def test_the_walk_sorts_ascending_so_an_insertion_cannot_hide_an_item() -> None:
    """Not cosmetic, and not something `FakeEmbyServer` can demonstrate --
    it sorts by its own recorded timestamp regardless of what is asked for.
    Pinned as a parameter because the reasoning is load-bearing and
    invisible otherwise: under a *stable ascending* sort, an item added
    mid-walk lands at the end, so it cannot shift an unread item backwards
    past a page boundary that has already been consumed. Descending does
    exactly that, and the walk would skip a real item with no error.

    `EnableTotalRecordCount` rides along here because the walk's early
    termination depends on the count actually being returned; Emby omits it
    unless asked.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    captured: list[httpx.Request] = []
    original = server.handle

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return original(request)

    adapter = _on(spy)
    try:
        _ = [entry async for entry in adapter.list_items()]
    finally:
        await adapter.aclose()
    listing = next(r for r in captured if r.url.path.endswith("/Items"))
    assert listing.url.params["SortBy"] == "DateCreated"
    assert listing.url.params["SortOrder"] == "Ascending"
    assert listing.url.params["EnableTotalRecordCount"] == "true"


async def test_a_server_that_reports_no_total_does_not_truncate_the_walk() -> None:
    """The one failure this port exists to make impossible, and the reason
    the termination check is guarded on a *positive* count.

    A server that omits `TotalRecordCount` -- or reports 0 while returning
    items, which some Emby builds do for a filtered query -- would, under a
    `start >= total` check alone, end the walk after page one. The
    reconciler cannot tell that from "the library ended" and would mark
    every unread item `available = false`. `FakeEmbyServer` always reports
    a correct positive count, so nothing else in this suite can catch it.
    """
    pages = [
        {"Items": [{"Id": "movie-0", "Type": "Movie", "Name": "A"}], "TotalRecordCount": 0},
        {"Items": [{"Id": "movie-1", "Type": "Movie", "Name": "B"}]},
        {"Items": [], "TotalRecordCount": 0},
    ]
    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        index = len(served)
        served.append(index)
        return httpx.Response(200, json=pages[min(index, len(pages) - 1)])

    adapter = _on(handler)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert seen == ["movie-0", "movie-1"]


async def test_the_walk_sends_a_widened_delta_cursor() -> None:
    """The port promises `since` is inclusive and Emby's own comparison is
    unverified, so the parameter goes out one second early -- see
    `mapping.emby_datetime`."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T1)
    adapter = _adapter(server)
    try:
        seen = [item.external_id async for item in adapter.list_items(since=T1)]
    finally:
        await adapter.aclose()
    assert seen == ["movie-0"]


async def test_the_delta_cursor_actually_narrows_the_window() -> None:
    """The other half of the previous test: inclusive at the boundary, and
    still a filter rather than a no-op."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.add_item(_movie(1), T1)
    adapter = _adapter(server)
    try:
        seen = {item.external_id async for item in adapter.list_items(since=T1)}
    finally:
        await adapter.aclose()
    assert seen == {"movie-1"}


async def test_a_library_walk_and_a_watch_state_walk_filter_on_different_stamps() -> None:
    """A library edit and a watch-state change do not touch the same Emby
    timestamp, so the two walks cannot share one parameter. Sending
    `MinDateLastSaved` for a watch-state delta would miss every item whose
    *only* change was being marked played -- which is the entire population
    that walk exists to find."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    captured: list[httpx.Request] = []
    original = server.handle

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return original(request)

    adapter = _on(spy)
    try:
        _ = [entry async for entry in adapter.list_items(since=T1)]
        _ = [entry async for entry in adapter.watch_state(since=T1)]
    finally:
        await adapter.aclose()
    listings = [r for r in captured if r.url.path.endswith("/Items")]
    assert "MinDateLastSaved" in listings[0].url.params
    assert "MinDateLastSavedForUser" in listings[1].url.params
    assert "MinDateLastSavedForUser" not in listings[0].url.params


async def test_an_unmodelled_item_type_in_a_page_is_skipped_not_fatal() -> None:
    """A server that ignores `IncludeItemTypes` returns Seasons and
    BoxSets. Aborting a 94,395-item reconcile over one of them would be
    worse than ignoring it.

    The second page is empty rather than a repeat of the first: a handler
    that serves the same page forever turns any break in the walk's
    termination into a hung test rather than a failing one, and a suite
    that hangs is worse than one that fails. Found while mutation-testing
    the `TotalRecordCount` guard, which did exactly that.
    """
    served: list[int] = []
    page = {
        "Items": [
            {"Id": "season-1", "Type": "Season", "Name": "Season 1"},
            {"Id": "movie-9", "Type": "Movie", "Name": "Kept"},
        ],
        "TotalRecordCount": 2,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        served.append(1)
        if len(served) > 1:
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 2})
        return httpx.Response(200, json=page)

    adapter = _on(handler)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert seen == ["movie-9"]


async def test_a_listing_with_no_items_array_is_malformed() -> None:
    """Not a truncation: a caller has to be able to tell "the library ended"
    from "the response was not a listing at all"."""

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(200, json={"TotalRecordCount": 3})

    adapter = _on(handler)
    try:
        with pytest.raises(PortDataMalformed):
            _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()


# --- get_item --------------------------------------------------------


async def test_get_item_raises_rather_than_returning_none_on_a_server_error() -> None:
    """The distinction the port's docstring calls out: `None` means the
    item was deleted, and a 500 does not mean that. Reporting it as `None`
    marks a healthy item unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(500, text="boom")

    adapter = _on(handler)
    try:
        with pytest.raises(PortUnavailable):
            await adapter.get_item("movie-0")
    finally:
        await adapter.aclose()


async def test_an_empty_object_for_an_unknown_id_is_a_deletion_not_an_item() -> None:
    """Some Emby builds answer an unknown id with `200 {}` rather than 404.
    An item with no `Id` cannot be upserted on `(source_id, external_id)`,
    so treating it as present would write a nameless row; `None` is the
    honest answer and is what a 404 already means."""

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(200, json={})

    adapter = _on(handler)
    try:
        assert await adapter.get_item("never-existed") is None
        assert await adapter.stream_targets("never-existed") == []
    finally:
        await adapter.aclose()


# --- watch state -----------------------------------------------------


async def test_watch_state_is_attributed_to_the_authenticated_user() -> None:
    """`source_user_id` exists so a household with two Emby users is a
    migration rather than a silent mis-attribution. Leaving it `None` when
    the id is right there is throwing that away."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.set_watch_state(
        SourceWatchState(external_id="movie-0", position_seconds=600, played=False)
    )
    adapter = _adapter(server)
    try:
        states = [state async for state in adapter.watch_state()]
    finally:
        await adapter.aclose()
    assert states[0].source_user_id == USER_ID
    assert states[0].position_seconds == 600


async def test_push_writes_the_position_before_the_played_flag() -> None:
    """Load-bearing order, asserted two ways. Emby clears an item's resume
    position when it is marked played, so the reverse order leaves a
    just-finished film resumable at the last reported second -- which is how
    it reappears in Continue Watching. The request order pins the mechanism;
    the resulting state pins the consequence."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=True)
        )
    finally:
        await adapter.aclose()
    writes = [
        entry for entry in server.requests if "PlayingItems" in entry or "PlayedItems" in entry
    ]
    assert len(writes) == 2
    assert "PlayingItems" in writes[0]
    assert "PlayedItems" in writes[1]
    assert server.recorded_watch_state("movie-0") == (0, True)


async def test_push_deletes_the_played_flag_when_unplaying() -> None:
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=False)
        )
    finally:
        await adapter.aclose()
    assert any(entry.startswith("DELETE ") and "PlayedItems" in entry for entry in server.requests)
    assert server.recorded_watch_state("movie-0") == (600, False)


async def test_a_negative_position_is_clamped_rather_than_sent_upstream() -> None:
    """`WatchStateUpdate` is a plain dataclass with no validation, so a
    caller can hand this a negative position -- and Emby's `PositionTicks`
    is unsigned. Clamped here rather than trusted, because the alternative
    is a 400 on a write-back that PRD 03 then retries forever."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=-90, played=False)
        )
    finally:
        await adapter.aclose()
    assert server.recorded_watch_state("movie-0") == (0, False)


# --- verify ----------------------------------------------------------


async def test_verify_reports_the_server_version() -> None:
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.reachable is True
    assert status.authenticated is True
    assert status.server_version == SERVER_VERSION
    assert status.push_available is None


async def test_verify_separates_unreachable_from_bad_credentials() -> None:
    """The whole reason the public info endpoint is probed first. With one
    authenticated call there is no way to tell a dead host from a wrong
    password, which is exactly what PRD 07's 🔶 was about."""
    server = FakeEmbyServer()
    server.reject_credentials()
    adapter = _adapter(server)
    try:
        bad_credentials = await adapter.verify()
        server.offline = True
        unreachable = await adapter.verify()
    finally:
        await adapter.aclose()
    assert (bad_credentials.reachable, bad_credentials.authenticated) == (True, False)
    assert (unreachable.reachable, unreachable.authenticated) == (False, False)


async def test_verify_reports_a_rate_limited_source_as_reachable() -> None:
    """A 429 means something answered, so the host is up -- and reporting it
    as unreachable would send an operator hunting a network fault that does
    not exist. `PortRateLimited` is a subclass of `UsherPortError`, so this
    only holds because it is caught first; the ordering is the test."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    adapter = _on(handler)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert (status.reachable, status.authenticated) == (True, False)


async def test_verify_never_leaks_the_password_into_its_detail() -> None:
    """`SourceStatus.detail` is rendered by `GET /admin/sources/{id}/status`
    straight into an admin response body."""
    server = FakeEmbyServer()
    server.reject_credentials()
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.detail is not None
    assert "correct-horse-battery" not in status.detail


# --- push, lifecycle, concurrency ------------------------------------


async def test_push_is_not_supported_yet_and_says_so() -> None:
    """PRD 03's documented fallback: an adapter with no socket reports
    `supports_push = False` and the reconciler covers the gap. M5 builds
    the socket; nothing here pretends to."""
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        assert adapter.supports_push is False
        with pytest.raises(SourceNotSupported):
            async with adapter.events():
                pass
    finally:
        await adapter.aclose()


async def test_aclose_closes_a_client_it_created_and_leaves_an_injected_one() -> None:
    """`EmbyAdapter` is normally constructed with no client and owns the one
    it makes. A test (and, later, a pooled registry) injects one and keeps
    ownership; closing someone else's client out from under them is the
    same mistake the bulk adapters' no-op `aclose` exists to avoid."""
    owned = EmbyAdapter(SOURCE, CREDENTIALS)
    await owned.aclose()
    assert owned._client.is_closed is True

    server = FakeEmbyServer()
    injected = httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=injected)
    await adapter.aclose()
    assert injected.is_closed is False
    await injected.aclose()


async def test_a_closed_adapter_does_not_reach_the_network_at_all() -> None:
    """`aclose()` must stop the adapter, not just the client it may not own.

    The plan predicted the contract's
    `test_operations_after_aclose_raise_port_unavailable` would catch a
    missing closed-check in `EmbySession.user_id()`. It does not, verified
    by mutation: `_fetch` calls `user_id()` first, and with the check gone
    that call *authenticates successfully* against the still-open injected
    transport before `request()`'s own check raises. The right answer still
    comes out, so nothing fails -- while a closed adapter has quietly minted
    a fresh Emby session, against an upstream measured at 1-5 s per call.

    So the assertion here is on the *absence of traffic*, which is the part
    that is actually unprotected: a closed adapter authenticates zero times.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    client = httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=client)
    try:
        await adapter.aclose()
        with pytest.raises(PortUnavailable):
            await adapter.get_item("movie-0")
    finally:
        await client.aclose()
    assert server.authentications == 0
    assert server.requests == []


async def test_concurrent_expired_sessions_produce_one_authentication() -> None:
    """The contract asserts this too, but cannot force it: over a plain
    `httpx.MockTransport` nothing ever really awaits, so the event loop
    tends to run one gathered call all the way through its own re-auth
    before starting the next, and every other call then observes an already
    fresh token. Group C proved that exact test passes with the
    single-flight lock deleted.

    So this is the adapter-level version with a transport that genuinely
    sleeps, and it asserts on observed overlap so it cannot quietly stop
    being concurrent. It is a different path from the session's own test:
    `_fetch` takes the session lock twice per call -- `user_id()` and then
    `request()` -- with a window in between that a single-lock test never
    reaches.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    transport = SlowTransport(server.handle)
    client = httpx.AsyncClient(transport=transport, base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=client)
    try:
        assert await adapter.get_item("movie-0") is not None
        before = server.authentications
        server.expire_session()
        results = await asyncio.gather(*(adapter.get_item("movie-0") for _ in range(4)))
    finally:
        await adapter.aclose()
        await client.aclose()
    assert all(result is not None for result in results)
    assert transport.max_in_flight >= 2, (
        f"test did not force real concurrency (max_in_flight={transport.max_in_flight}); "
        "not a meaningful run"
    )
    assert server.authentications - before == 1, (
        f"SINGLE-FLIGHT VIOLATED: {server.authentications - before} authentications for 4 "
        f"provably-concurrent expired sessions (max_in_flight={transport.max_in_flight})"
    )


async def test_a_rate_limited_walk_surfaces_the_retry_hint() -> None:
    """A 429 mid-walk must reach the caller as `PortRateLimited` carrying
    the upstream's own hint, not as a generic failure -- PRD 08's retry
    policy backs off on the hint when there is one."""

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(429, headers={"retry-after": "17"})

    adapter = _on(handler)
    try:
        with pytest.raises(PortRateLimited) as exc_info:
            _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert exc_info.value.retry_after == 17.0
