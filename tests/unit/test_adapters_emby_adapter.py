"""EmbyAdapter behaviours the source-agnostic contract cannot express."""

import asyncio
import io
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_fixtures import load_emby_fixture
from tests.fakes.emby_server import SERVER_VERSION, USER_ID, FakeEmbyServer
from tests.fakes.push_connection import FakePushConnection, FakePushConnector
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby.adapter import MAX_PAGES, EmbyAdapter
from usher.adapters.emby.push import SUBSCRIBE_FRAME
from usher.adapters.emby.session import redact_path
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable
from usher.ports.source import (
    SourceEventKind,
    SourceItem,
    SourceItemKind,
    SourceWatchState,
    WatchStateUpdate,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
# Every `async for` over a push channel is bounded, so an iterator that
# stopped yielding instead of raising fails its own case rather than hanging
# the suite -- `pytest-timeout` is deliberately not a dependency and the
# bound belongs to the cases that need it.
BOUND = 5.0
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


def _on(
    handler: Callable[[httpx.Request], httpx.Response], *, max_pages: int = MAX_PAGES
) -> EmbyAdapter:
    """An adapter over a hand-written handler, for shapes `FakeEmbyServer` will not produce."""
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SOURCE.base_url),
        max_pages=max_pages,
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
    """A missing `Fields=MediaSources` costs every quality fact and raises nothing.

    Items come back with no container, no codec and no HDR. `Recursive` is pinned for
    the same reason from the other direction: without it Emby answers with the top level
    of each library — folders, not films — and again nothing raises.
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


async def test_the_walk_asks_for_a_total_order_ascending() -> None:
    """Both sort keys, pinned as literal parameters.

    Neither is demonstrable from this side of the wire.
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
    sort_by = listing.url.params["SortBy"].split(",")
    assert sort_by[0] == "DateCreated"
    assert len(sort_by) > 1, "DateCreated alone is not a total order; paging over it drops items"
    assert listing.url.params["SortOrder"] == "Ascending"
    assert listing.url.params["EnableTotalRecordCount"] == "true"


async def test_tied_timestamps_do_not_drop_items_out_of_the_paging_window() -> None:
    """`DateCreated` ties are the normal case, so the sort needs a second key.

    A bulk import stamps a whole library inside one second. `StartIndex`/`Limit` paging
    is only safe over a total order: a server free to break ties differently between two
    page requests reshuffles the window under the cursor and items fall through the gap.
    Duplicates mask the loss exactly, so only comparing sets finds it — and the
    reconciler would mark the missing films `available = false`.
    """
    server = FakeEmbyServer(page_size=2)
    for index in range(10):
        server.add_item(_movie(index), T0)
    adapter = _adapter(server, page_size=2)
    try:
        seen = {item.external_id async for item in adapter.list_items()}
    finally:
        await adapter.aclose()
    assert seen == {f"movie-{index}" for index in range(10)}


async def test_a_server_that_reports_no_total_does_not_truncate_the_walk() -> None:
    """The termination check is guarded on a positive count.

    A server that omits `TotalRecordCount`, or reports 0 while returning items as some
    builds do for a filtered query, would end the walk after page one under a
    `start >= total` check alone — and the reconciler cannot tell that from "the library
    ended". `FakeEmbyServer` always reports a correct positive count, so nothing else in
    this suite can catch it.
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
    """The port promises `since` is inclusive, so the parameter goes out one second early.

    Emby's own comparison is unknown; see `mapping.emby_datetime`.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T1)
    adapter = _adapter(server)
    try:
        seen = [item.external_id async for item in adapter.list_items(since=T1)]
    finally:
        await adapter.aclose()
    assert seen == ["movie-0"]


async def test_the_delta_cursor_actually_narrows_the_window() -> None:
    """Inclusive at the boundary, and still a filter rather than a no-op."""
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
    """A library edit and a watch-state change touch different Emby timestamps.

    Sending `MinDateLastSaved` for a watch-state delta would miss every item whose only
    change was being marked played, which is the entire population that walk exists to
    find.
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
        _ = [entry async for entry in adapter.list_items(since=T1)]
        _ = [entry async for entry in adapter.watch_state(since=T1)]
    finally:
        await adapter.aclose()
    listings = [r for r in captured if r.url.path.endswith("/Items")]
    assert "MinDateLastSaved" in listings[0].url.params
    assert "MinDateLastSavedForUser" in listings[1].url.params
    assert "MinDateLastSavedForUser" not in listings[0].url.params


@pytest.mark.parametrize("walk", ["list_items", "watch_state"])
async def test_a_naive_since_cursor_never_reaches_the_wire(walk: str) -> None:
    """Both walks, because both take a `since` and spell it into a different parameter.

    A naive cursor is a caller bug that shifts the delta window by the host's UTC offset
    and reports nothing: the walk simply returns fewer items than it should, forever.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    naive = datetime(2026, 7, 20, 12, 0, 0)
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            _ = [entry async for entry in getattr(adapter, walk)(since=naive)]
    finally:
        await adapter.aclose()
    assert not [entry for entry in server.requests if entry.endswith("/Items")]


async def test_an_unmodelled_item_type_in_a_page_is_skipped_not_fatal() -> None:
    """A server that ignores `IncludeItemTypes` returns Seasons and BoxSets.

    Aborting a whole-library reconcile over one of them would be worse than ignoring it.
    The second page is empty rather than a repeat of the first, because a handler that
    serves the same page forever turns any break in the walk's termination into a hung
    test rather than a failing one.
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


async def test_a_server_that_ignores_start_index_ends_the_walk_rather_than_running_forever() -> (
    None
):
    """A proxy that strips `StartIndex` makes an unbounded walk immortal.

    Every page comes back full, so the empty-page check never fires and `start` never
    passes a `TotalRecordCount` that never arrives. The handler here relents after far
    more pages than the bound allows, so removing the bound fails this case rather than
    hanging the suite.
    """
    served: list[int] = []
    page = {"Items": [{"Id": "movie-0", "Type": "Movie", "Name": "A"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        served.append(1)
        # The escape hatch. Without it, deleting the bound hangs this test
        # instead of failing it, and a suite that hangs is worse than one
        # that fails.
        if len(served) > 20:
            return httpx.Response(200, json={"Items": []})
        return httpx.Response(200, json=page)

    adapter = _on(handler, max_pages=3)
    try:
        with pytest.raises(PortDataMalformed, match="StartIndex"):
            _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert len(served) == 3


async def test_the_walk_advances_by_what_it_was_served_not_by_what_it_asked_for() -> None:
    """A server may return fewer items than `Limit`, so the cursor advances by what arrived.

    A page thinned by a filter, a per-library cap, or the last page of a library.
    `start += self._page_size` jumps past exactly the difference on the next request —
    silently, with no error and no empty page — and the reconciler marks every skipped
    item `available = false`.
    """
    library = [{"Id": f"movie-{index}", "Type": "Movie", "Name": f"M{index}"} for index in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        start = int(request.url.params["StartIndex"])
        return httpx.Response(
            200,
            json={"Items": library[start : start + 1], "TotalRecordCount": len(library)},
        )

    adapter = _on(handler)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert seen == ["movie-0", "movie-1", "movie-2"]


async def test_the_default_page_size_is_what_goes_out_as_the_limit() -> None:
    """The default `page_size` has its own case, since every other test passes one.

    A default of 1 would fail nothing here and turn a whole-library reconcile into one
    request per item against an upstream that answers in seconds.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        captured.append(request)
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    adapter = _on(handler)
    try:
        _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert captured[0].url.params["Limit"] == "200"


async def test_a_page_entry_that_is_not_an_object_is_skipped_not_fatal() -> None:
    """`Items` is a list of objects until a server answers with a list of something else.

    `to_source_item` calls `.get` on whatever it is handed, and an `AttributeError` is
    not an error any caller written against `usher.ports.errors` can catch — so one junk
    entry would abort a whole-library reconcile.
    """
    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        served.append(1)
        if len(served) > 1:
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 2})
        return httpx.Response(
            200,
            json={
                "Items": ["not-an-object", {"Id": "movie-9", "Type": "Movie", "Name": "Kept"}],
                "TotalRecordCount": 2,
            },
        )

    adapter = _on(handler)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert seen == ["movie-9"]


async def test_a_listing_with_no_items_array_is_malformed() -> None:
    """Not a truncation: a caller must tell "the library ended" from "not a listing"."""

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


async def test_a_watch_state_walk_resumes_from_the_start_index_it_is_given() -> None:
    """A resumed walk asks Emby for the page it stopped at rather than for page one.

    The walk's order is `SortBy=DateCreated,SortName` ascending and `DateCreated` is
    immutable, so the prefix already walked does not reorder between attempts.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        requested.append(request.url.params["StartIndex"])
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 51_000})

    adapter = _on(handler)
    try:
        states = [one async for one in adapter.watch_state(start_index=50_000)]
    finally:
        await adapter.aclose()

    assert states == []
    assert requested == ["50000"], "the walk asked for page one instead of resuming"


async def test_a_resumed_watch_state_walk_re_yields_what_it_dropped() -> None:
    """The port's `start_index` and Emby's `StartIndex` are not the same quantity."""
    entries = [
        {"Id": f"movie-{index}", "Type": "Movie", "Name": f"m{index}"}
        | ({} if index in (1, 3) else {"UserData": {"PlaybackPositionTicks": 0, "Played": False}})
        for index in range(6)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        start = int(request.url.params["StartIndex"])
        return httpx.Response(
            200, json={"Items": entries[start:], "TotalRecordCount": len(entries)}
        )

    adapter = _on(handler)
    try:
        first = [one async for one in adapter.watch_state()]
        resumed = [one async for one in adapter.watch_state(start_index=len(first))]
    finally:
        await adapter.aclose()

    assert [one.external_id for one in first] == ["movie-0", "movie-2", "movie-4", "movie-5"], (
        "the premise: the two entries with no UserData are dropped, not yielded as zero states"
    )
    # Not `[]`: the walk yielded 4 and resumes at 4, so an exact offset into what
    # it yields would be exhausted. `StartIndex=4` is an offset into the six the
    # server holds instead.
    assert [one.external_id for one in resumed] == ["movie-4", "movie-5"]
    assert set(one.external_id for one in resumed) <= set(one.external_id for one in first), (
        "the resume landed *past* its checkpoint and skipped a record, which is the "
        "one direction the idempotent upsert cannot absorb"
    )


async def test_an_item_walk_always_starts_at_the_beginning() -> None:
    """`list_items` shares `_walk` and must not inherit the watch lane's resume point.

    The item lanes have a working cursor and restart from it.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        requested.append(request.url.params["StartIndex"])
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    adapter = _on(handler)
    try:
        assert [one async for one in adapter.list_items()] == []
    finally:
        await adapter.aclose()
    assert requested == ["0"]


# --- get_item --------------------------------------------------------


async def test_get_item_raises_rather_than_returning_none_on_a_server_error() -> None:
    """`None` means the item was deleted, and a 500 does not mean that.

    Reporting a 500 as `None` marks a healthy item unavailable.
    """

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


async def test_get_item_returns_none_for_a_404() -> None:
    """A 404 is a value, not an error, and this is its unit-level pin.

    `None` means "gone, mark it unavailable"; raising for the same response means the
    reconciler never learns the file was deleted. The contract suite reaches this only
    through `FakeEmbyServer`, so the literal `status_code == 404` branch could be
    deleted without failing anything else here.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(404, json={"Error": "Not Found"})

    adapter = _on(handler)
    try:
        assert await adapter.get_item("movie-0") is None
        assert await adapter.stream_targets("movie-0") == []
    finally:
        await adapter.aclose()


async def test_an_empty_object_for_an_unknown_id_is_a_deletion_not_an_item() -> None:
    """Some Emby builds answer an unknown id with `200 {}` rather than 404.

    An item with no `Id` cannot be upserted on `(source_id, external_id)`, so treating
    it as present would write a nameless row. `None` is what a 404 already means.
    """

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


@pytest.mark.parametrize(
    ("container", "width", "height"),
    [
        # A lesser version listed first. Under `MediaSources[0]` the 4K
        # file is unreachable and `/play` hands back a 1080p target.
        ("mp4", 1920, 1080),
        # A transcode-only version listed first: no container, so under
        # `MediaSources[0]` the whole item reports "not playable here".
        (None, 3840, 2160),
    ],
)
async def test_a_version_listed_first_does_not_win_by_being_first(
    container: str | None, width: int, height: int
) -> None:
    """The media-source selection through a real walk and a real `get_item`.

    A fixture cannot reach the choice: each has exactly one `MediaSources` entry, so
    only a server rendering more than one exercises the selection at all.
    """
    server = FakeEmbyServer()
    best = replace(_movie(0), width=3840, height=2160)
    server.add_item(best, T0)
    server.add_alternate_version(best.external_id, container=container, width=width, height=height)
    adapter = _adapter(server)
    try:
        item = await adapter.get_item(best.external_id)
        targets = await adapter.stream_targets(best.external_id)
    finally:
        await adapter.aclose()
    assert item is not None
    assert (item.container, item.width, item.height) == ("mkv", 3840, 2160)
    assert targets, "an item with a direct-playable version reported no way to play it"
    assert (targets[0].container, targets[0].resolution) == ("mkv", "3840x2160")


async def test_an_external_id_stays_inside_one_path_segment() -> None:
    """An `external_id` is interpolated into a request path, so it has to be quoted.

    Unquoted it is a path traversal: httpx normalises `..` the way a browser does, so
    `get_item("../../System/Info")` resolves to `GET /Users/System/Info` and
    `push_watch_state` aims two writes at an endpoint of the caller's choosing.
    `params=` neutralises the same trick in a query string; a path segment needs quoting.
    """
    hostile = "../../System/Info"
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        captured.append(request)
        return httpx.Response(200, json={})

    adapter = _on(handler)
    try:
        await adapter.get_item(hostile)
        await adapter.stream_targets(hostile)
        await adapter.push_watch_state(hostile, WatchStateUpdate(position_seconds=1, played=True))
    finally:
        await adapter.aclose()
    # `raw_path`, not `path`: httpx's `URL.path` percent-*decodes*, so it
    # reports a correctly-escaped segment and a traversal identically.
    paths = [request.url.raw_path.decode().split("?")[0] for request in captured]
    escaped = quote(hostile, safe="")
    assert paths == [
        f"/Users/{USER_ID}/Items/{escaped}",
        f"/Users/{USER_ID}/Items/{escaped}",
        f"/Users/{USER_ID}/Items/{escaped}/UserData",
        f"/Users/{USER_ID}/PlayedItems/{escaped}",
    ]
    assert "/System/Info" not in "".join(paths)


# --- watch state -----------------------------------------------------


async def test_watch_state_is_attributed_to_the_authenticated_user() -> None:
    """`source_user_id` makes a household with two Emby users a migration, not a mix-up.

    Leaving it `None` when the id is right there throws that away.
    """
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


async def test_get_watch_state_uses_the_single_item_route() -> None:
    """One request against `/Users/{u}/Items/{id}`, the route that carries the play facts.

    An implementation that walked the listing instead would be wrong and would pay a
    whole library to answer one question. The listing route's path is `/Users/{u}/Items`
    exactly, and `server.requests` holds no query string — which is why an `"/Items?"`
    spelling would match nothing and pass against an adapter that walked everything.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.set_watch_state(
        SourceWatchState(
            external_id="movie-0",
            position_seconds=1840,
            played=True,
            play_count=7,
            last_played_at=datetime(2026, 7, 20, 21, 4, tzinfo=UTC),
        )
    )
    adapter = _adapter(server)
    server.requests.clear()
    try:
        state = await adapter.get_watch_state("movie-0")
    finally:
        await adapter.aclose()
    assert state is not None
    assert state.play_count == 7
    assert state.last_played_at == datetime(2026, 7, 20, 21, 4, tzinfo=UTC)
    assert state.position_seconds == 1840
    assert state.played is True
    assert state.source_user_id == USER_ID
    assert [entry for entry in server.requests if entry.endswith("/Items")] == []
    assert f"GET /Users/{USER_ID}/Items/movie-0" in server.requests


async def test_get_watch_state_is_labelled_as_its_own_operation() -> None:
    """The request metric buckets by `op`, and `get_watch_state` needs its own label.

    It shares `_fetch`'s route with `get_item`, and a history backfill is thousands of
    single-item reads — counting them as `get_item` makes "how slow is `get_item`"
    answer a different question on the nights the backfill runs.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.get_watch_state("movie-0")
    finally:
        await adapter.aclose()

    labels = {
        span.attributes["usher.op"]
        for span in exporter.get_finished_spans()
        if span.name == "source.request" and span.attributes is not None
    }
    assert labels == {"get_watch_state"}


async def test_get_watch_state_returns_none_rather_than_raising_for_a_missing_item() -> None:
    """The same 404-is-a-value split `get_item` makes, through the same `_fetch`.

    A caller must never learn to tell a deletion from an outage by which method it
    called.
    """
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        assert await adapter.get_watch_state("never-existed") is None
    finally:
        await adapter.aclose()


async def test_get_watch_state_raises_rather_than_returning_none_on_a_server_error() -> None:
    """A 500 is not a deletion, pinned here because a harness cannot arrange a status.

    Reporting a 500 as `None` would let a struggling server look like an item that was
    never watched, and the merge downstream would believe it. `_authenticated` comes
    first deliberately: a handler answering 500 to every path, authentication included,
    also raises `PortUnavailable` — from the session rather than from `_fetch` — and
    would pass this while proving nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        return httpx.Response(500, text="boom")

    adapter = _on(handler)
    try:
        with pytest.raises(PortUnavailable):
            await adapter.get_watch_state("movie-0")
    finally:
        await adapter.aclose()


async def test_the_walk_reports_absent_play_history() -> None:
    """The listing route reports `PlayCount: 0` for a played item, so the walk reports absence.

    Position and played flag are correct in the listing and are asserted here too, so an
    adapter that answered `None` to everything does not pass by giving up.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.set_watch_state(
        SourceWatchState(
            external_id="movie-0",
            position_seconds=1840,
            played=True,
            play_count=7,
            last_played_at=datetime(2026, 7, 20, 21, 4, tzinfo=UTC),
        )
    )
    adapter = _adapter(server)
    try:
        states = [state async for state in adapter.watch_state()]
    finally:
        await adapter.aclose()
    assert len(states) == 1
    assert states[0].position_seconds == 1840
    assert states[0].played is True
    assert states[0].play_count is None
    assert states[0].last_played_at is None


async def test_push_writes_the_position_through_the_route_emby_accepts() -> None:
    """`UserData` is the route that writes a resume position without a play session.

    The `Progress` routes are session-scoped playback reporting and answer 400 for every
    body shape, because Usher is not playing anything and there is no play session for
    them to key off. The body is JSON, not query parameters.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=False)
        )
    finally:
        await adapter.aclose()
    assert f"POST /Users/{USER_ID}/Items/movie-0/UserData" in server.requests
    assert not any("PlayingItems" in entry for entry in server.requests)
    assert server.user_data_writes == [{"PlaybackPositionTicks": 6_000_000_000, "Played": False}]


async def test_push_names_played_explicitly_rather_than_omitting_it() -> None:
    """The write names `Played` explicitly, because unset fields take C# defaults.

    A body carrying only `PlaybackPositionTicks` flips an item's `Played` from `true` to
    `false`. `PlayCount` and `LastPlayedDate` survive the same omission; `Played` does
    not.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=True)
        )
    finally:
        await adapter.aclose()
    assert "Played" in server.user_data_writes[0]


async def test_push_writes_the_position_before_the_played_flag() -> None:
    """Load-bearing order, asserted two ways.

    `POST /Users/{user}/PlayedItems/{item}` clears the item's resume position while
    setting `Played`, so the reverse order leaves a just-finished film resumable at the
    last reported second — which is how it reappears in Continue Watching. The request
    order pins the mechanism; the resulting state pins the consequence.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=True)
        )
    finally:
        await adapter.aclose()
    writes = [entry for entry in server.requests if "UserData" in entry or "PlayedItems" in entry]
    assert len(writes) == 2
    assert "UserData" in writes[0]
    assert "PlayedItems" in writes[1]
    assert server.recorded_watch_state("movie-0") == (0, True)


async def test_reporting_a_position_does_not_reach_the_played_route() -> None:
    """`DELETE /Users/{user}/PlayedItems/{item}` is destructive well beyond its name.

    It resets `PlayCount`, clears `LastPlayedDate` and clears a non-zero resume
    position, so a walk reporting "resumable at 20 minutes, not played" through it would
    erase the household's play history and throw away the position it was called to
    write. The unplayed path is one `UserData` write carrying `Played: false`, and the
    surviving history is read back through `get_watch_state` — the walk answers absence
    there, so reading it from the walk would assert nothing.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.set_watch_state(
        SourceWatchState(
            external_id="movie-0",
            position_seconds=0,
            played=True,
            play_count=3,
            last_played_at=T0,
        )
    )
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=False)
        )
        states = [state async for state in adapter.watch_state()]
        after = await adapter.get_watch_state("movie-0")
    finally:
        await adapter.aclose()
    assert not any("PlayedItems" in entry for entry in server.requests)
    assert (states[0].position_seconds, states[0].played) == (600, False)
    assert after is not None
    assert (after.play_count, after.last_played_at) == (3, T0)


async def test_a_negative_position_is_clamped_rather_than_sent_upstream() -> None:
    """`WatchStateUpdate` validates nothing, so a negative position is clamped here.

    Emby's `PositionTicks` is unsigned, and the alternative is a 400 on a write-back
    that the caller then retries forever.
    """
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


async def test_verify_prefers_the_authenticated_version_and_falls_back_to_the_public_one() -> None:
    """`_version_of(info) or version` needs both sides pinned, one case each.

    The fake answers both probes with the same string, so returning either one
    unconditionally passes. The authenticated `/System/Info` is the better source and
    wins when it has a version; some builds answer it without one, and the fallback is
    the difference between an admin panel showing a version and showing nothing.
    """

    def serving(public: dict[str, str], info: dict[str, str]) -> EmbyAdapter:
        def handler(request: httpx.Request) -> httpx.Response:
            authenticated = _authenticated(request)
            if authenticated is not None:
                return authenticated
            return httpx.Response(200, json=public if "Public" in request.url.path else info)

        return _on(handler)

    adapter = serving({"Version": "4.0.0.0"}, {"Version": "4.9.5.0"})
    try:
        assert (await adapter.verify()).server_version == "4.9.5.0"
    finally:
        await adapter.aclose()

    adapter = serving({"Version": "4.0.0.0"}, {"ServerName": "no version here"})
    try:
        assert (await adapter.verify()).server_version == "4.0.0.0"
    finally:
        await adapter.aclose()


async def test_verify_separates_unreachable_from_bad_credentials() -> None:
    """The public info endpoint is probed first, so the two failures are distinguishable.

    With one authenticated call there is no way to tell a dead host from a wrong
    password.
    """
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
    """A 429 means something answered, so the host is up.

    Reporting it as unreachable would send an operator hunting a network fault that does
    not exist. `PortRateLimited` is a subclass of `UsherPortError`, so this only holds
    because it is caught first; the ordering is the test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    adapter = _on(handler)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert (status.reachable, status.authenticated) == (True, False)


@pytest.mark.parametrize("failure", [httpx.StreamError("gone"), RuntimeError("client is closed")])
async def test_verify_reports_a_transport_failure_rather_than_raising_it(
    failure: Exception,
) -> None:
    """`verify` returns rather than raises for every expected failure, transport included.

    Its one caller renders those states rather than handling them. Neither of these is
    an `httpx.HTTPError`, so both can escape `EmbySession._send`'s translation and sail
    through `verify`'s `except UsherPortError`, leaving the admin endpoint to return 500
    instead of rendering "unreachable".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    adapter = _on(handler)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert (status.reachable, status.authenticated) == (False, False)


async def test_verify_never_leaks_the_password_into_its_detail() -> None:
    """`SourceStatus.detail` is rendered straight into an admin response body."""
    server = FakeEmbyServer()
    server.reject_credentials()
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.detail is not None
    assert "correct-horse-battery" not in status.detail


# --- verify: the administrator check ---------------------------------


async def test_verify_reports_a_non_admin_account() -> None:
    server = FakeEmbyServer()
    server.is_administrator = False
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.authenticated is True
    assert status.is_administrator is False


async def test_verify_reports_an_admin_account() -> None:
    """Needing no admin privileges is a permission, not a constraint.

    Nothing stops an operator pasting admin credentials into `POST /admin/sources`, and
    that token opens a long-lived socket as well as riding in every playback URL. This
    is the check that makes it visible.
    """
    server = FakeEmbyServer()
    server.is_administrator = True
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.is_administrator is True


async def test_verify_leaves_the_role_undetermined_when_the_user_route_fails() -> None:
    """A status screen must render what it knows, so a failed probe narrows the answer.

    `GET /Users/Me` answers 500 on a real build and a future one could do the same for
    `GET /Users/{id}`, so this branch degrades exactly as every other branch of
    `verify()` does.
    """
    server = FakeEmbyServer()
    server.user_route_fails = True
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.reachable is True
    assert status.authenticated is True
    assert status.is_administrator is None


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param({}, id="no-IsAdministrator-key"),
        pytest.param({"IsAdministrator": "true"}, id="a-string-rather-than-a-bool"),
        pytest.param({"IsAdministrator": None}, id="an-explicit-null"),
        pytest.param("not-an-object", id="Policy-is-not-a-mapping"),
        pytest.param(None, id="no-Policy-at-all"),
    ],
)
async def test_a_policy_that_does_not_say_leaves_the_role_undetermined(policy: object) -> None:
    """A 200 whose `Policy` omits `IsAdministrator` answers `None`, not `False`.

    A 500 raises inside `json_body` long before any key is read, so this is the only
    shape that reaches the spelling `bool(policy.get("IsAdministrator"))` — and that is
    what a different build, a Jellyfin server or a body-rewriting proxy produces.
    `bool(None)` is `False`, which renders an unperformed check as a performed one.
    """
    user: dict[str, object] = {"Id": USER_ID, "Name": "usher"}
    if policy is not None:
        user["Policy"] = policy

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        if request.url.path == f"/Users/{USER_ID}":
            return httpx.Response(200, json=user)
        return httpx.Response(200, json={"Version": SERVER_VERSION})

    adapter = _on(handler)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.authenticated is True
    assert status.is_administrator is None


async def test_verify_warns_about_an_administrator_account_and_serves_it_anyway() -> None:
    """A log line, not a refusal, and the log line is the whole of the mitigation.

    An operator whose only working account is an administrator account still needs a
    catalog, and a `verify()` that raised would take the status route from "renders
    every state a source can be in" to "500s on the one state an operator most needs to
    see". The status still comes back authenticated.
    """
    server = FakeEmbyServer()
    server.is_administrator = True
    adapter = _adapter(server)
    sink = io.StringIO()
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        status = await adapter.verify()
    finally:
        logger.remove()
        await adapter.aclose()
    logged = sink.getvalue()
    assert "administrator" in logged
    assert SOURCE.name in logged
    assert "correct-horse-battery" not in logged
    assert (status.reachable, status.authenticated) == (True, True)


async def test_verify_says_nothing_about_an_ordinary_account() -> None:
    """The other half: a non-admin account produces no warning at all.

    A warning every operator sees on every poll of a correct source is one nobody reads.
    """
    server = FakeEmbyServer()
    server.is_administrator = False
    adapter = _adapter(server)
    sink = io.StringIO()
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        await adapter.verify()
    finally:
        logger.remove()
        await adapter.aclose()
    assert sink.getvalue() == ""


async def test_the_role_probe_reads_the_users_route_for_the_authenticated_user() -> None:
    """`GET /Users/Me` answers 500 on Emby 4.9.5.0, so the user id is interpolated.

    It is the id the session authenticated as, not a guess. Pinned as the request the
    adapter actually made, since a probe aimed at the wrong path returns `None` and
    reads exactly like a build that does not carry `Policy`.
    """
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        await adapter.verify()
    finally:
        await adapter.aclose()
    assert f"GET /Users/{USER_ID}" in server.requests


# --- push, lifecycle, concurrency ------------------------------------


class _Now:
    """A clock a test moves by hand.

    Frozen by default: `supports_push`, `verify()` and the channel's own
    staleness watchdog all read it, and a clock that advanced on its own
    would make a case about the *ledger* fail for a reason about time.
    """

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _push_adapter(
    server: FakeEmbyServer,
    connector: FakePushConnector,
    *,
    clock: Callable[[], float] | None = None,
    stale_after: float = 90.0,
) -> EmbyAdapter:
    """The real adapter with a fake socket connector and a real `EmbyPushChannel` between.

    The ledger, the subscribe frame and the watchdog are all the shipped ones.
    """
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url),
        push_connect=connector,
        push_stale_after_seconds=stale_after,
        push_poll_seconds=0.001,
        clock=clock or _Now(),
    )


async def test_supports_push_is_false_before_anything_is_opened() -> None:
    """The documented fallback, reached through the ledger rather than a hardcoded `False`.

    An adapter with no live channel is covered by the reconciler's nightly walk.
    """
    server = FakeEmbyServer()
    adapter = _push_adapter(server, FakePushConnector())
    try:
        assert adapter.supports_push is False
    finally:
        await adapter.aclose()


async def test_events_yields_what_arrives_and_flips_supports_push() -> None:
    """An open socket and a sent subscription are not yet a push channel.

    `supports_push` stays `False` until something is delivered: a handshake against a
    nonexistent path produces exactly this state, and the reconciler skips a source that
    answers `True` here.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_user_data_changed")))
    adapter = _push_adapter(server, FakePushConnector([connection]))
    try:
        async with adapter.events() as events:
            assert adapter.supports_push is False, (
                "the socket is open and nothing has arrived; ADR-0004's control "
                "handshake against a nonexistent path produced exactly this state"
            )
            assert connection.sent == [SUBSCRIBE_FRAME]
            event = await asyncio.wait_for(anext(aiter(events)), timeout=BOUND)
            assert event.kind is SourceEventKind.WATCH_STATE_CHANGED
            assert adapter.supports_push is True
    finally:
        await adapter.aclose()


async def test_supports_push_is_false_again_once_the_channel_closes() -> None:
    """A message counted on a socket nobody holds is not evidence about a socket.

    `PushHealth.connected` is the clause that says so, and the channel's own `finally`
    is what clears it.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    adapter = _push_adapter(server, FakePushConnector([connection]))
    try:
        async with adapter.events() as events:
            # `Sessions` maps to no event, so the iterator never yields --
            # and the frame is still counted, which is the whole reason an
            # idle library's channel reads as alive.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
            assert adapter.supports_push is True
        assert adapter.supports_push is False
        assert adapter.push_health.messages_received == 1
    finally:
        await adapter.aclose()


async def test_events_opens_a_fresh_connection_per_call_and_keeps_one_ledger() -> None:
    """A channel is one connection, and `events()` is called once per reconnect.

    The connector must be asked again rather than a live socket reused. The ledger is
    shared deliberately, which is what makes `messages_received` and `reconnects` the
    lane's history rather than one connection's. Named for the connection rather than
    the channel, because `EmbyPushChannel` holds no per-connection state and no
    assertion here could see the difference.
    """
    server = FakeEmbyServer()
    first, second = FakePushConnection(), FakePushConnection()
    first.deliver(json.dumps(load_emby_fixture("push_sessions")))
    second.deliver(json.dumps(load_emby_fixture("push_sessions")))
    connector = FakePushConnector([first, second])
    adapter = _push_adapter(server, connector)
    try:
        for connection in (first, second):
            async with adapter.events() as events:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
            assert connection.closed is True
        assert connector.attempts == 2
        assert connector.handed_out == [first, second]
        assert adapter.push_health.messages_received == 2
        # The port's own accessor, which is what a lane supervisor holding
        # a `SourceAdapter` can reach -- one reconnect for two opens, never
        # two, because the first open is not a reconnect.
        assert adapter.push_reconnects == 1
    finally:
        await adapter.aclose()


async def test_verify_reports_push_as_not_probed_before_a_channel_has_opened() -> None:
    """`False` is a claim and `None` is an absence, and a fresh adapter has only the second.

    `push_available=self._health.is_delivering(...)` turns "nobody has looked" into
    "push is broken" on every status screen for every source with no lane running.
    """
    server = FakeEmbyServer()
    adapter = _push_adapter(server, FakePushConnector())
    try:
        assert (await adapter.verify()).push_available is None
    finally:
        await adapter.aclose()


async def test_verify_reports_the_running_channels_health_and_opens_no_socket() -> None:
    """`verify()` never opens a channel of its own.

    A status screen a dashboard polls must not cost a socket per poll against a server
    that answers in seconds. It reports the health of the channel actually running.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    connector = FakePushConnector([connection])
    adapter = _push_adapter(server, connector)
    try:
        async with adapter.events() as events:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
            assert (await adapter.verify()).push_available is True
            # One connection, and `verify()` is not the thing that made it.
            assert connector.attempts == 1
        assert (await adapter.verify()).push_available is False
        assert connector.attempts == 1
    finally:
        await adapter.aclose()


async def test_supports_push_decays_on_a_channel_that_stopped_delivering() -> None:
    """The staleness clause read through `supports_push`, which the reconciler calls.

    `verify()` reaches `is_delivering` by its own route, so a case that only went
    through the status screen leaves this untested. A socket that delivered once and
    nothing since is not a push channel, and `websockets`' own `ping_timeout` cannot
    tell: a peer answering pongs while delivering nothing passes the keepalive.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    now = _Now()
    adapter = _push_adapter(server, FakePushConnector([connection]), clock=now, stale_after=90.0)
    try:
        async with adapter.events() as events:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
            assert adapter.supports_push is True
            now.value = 90.0
            assert adapter.supports_push is True
            now.value = 90.1
            assert adapter.supports_push is False
            assert adapter.push_health.connected is True, (
                "the socket is still open and still counted -- staleness is the "
                "only reason this reads False, which is the clause under test"
            )
    finally:
        await adapter.aclose()


async def test_verify_reports_a_stale_channel_as_unavailable() -> None:
    """The staleness clause, read through `verify()` rather than through the ledger.

    A socket that delivered once an hour ago and nothing since is not a push channel a
    status screen may call available.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    connector = FakePushConnector([connection])
    now = _Now()
    adapter = _push_adapter(server, connector, clock=now, stale_after=90.0)
    try:
        async with adapter.events() as events:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
            assert (await adapter.verify()).push_available is True
            now.value = 91.0
            assert (await adapter.verify()).push_available is False
    finally:
        await adapter.aclose()


async def test_closing_the_adapter_closes_a_channel_that_is_still_open() -> None:
    """`aclose()` resets the ledger, and only a still-open channel can show it.

    A lane mid-`async for` when the source is deleted. Closing the channel first would
    let `EmbyPushChannel.open`'s own `finally` clear `connected`, so `supports_push`
    would read `False` with or without `record_close()`. Without the reset a status
    screen reads `push_available: true` for a source deleted thirty seconds ago.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    adapter = _push_adapter(server, FakePushConnector([connection]))
    async with adapter.events() as events:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
        assert adapter.supports_push is True
        await adapter.aclose()
        assert adapter.supports_push is False
    assert connection.closed is True


async def test_events_after_close_raises_port_unavailable() -> None:
    """After `aclose`, every other method raises `PortUnavailable`.

    A closed adapter that handed out a channel would have that channel authenticate
    against a closed `httpx.AsyncClient`, which raises a bare `RuntimeError`.
    """
    server = FakeEmbyServer()
    adapter = _push_adapter(server, FakePushConnector())
    await adapter.aclose()
    with pytest.raises(PortUnavailable):
        async with adapter.events():
            pass


async def test_probe_push_reports_what_arrived_not_that_it_connected() -> None:
    """The probe reports delivery rather than the handshake.

    A probe that reported the handshake would report success against a nonexistent path,
    which is what this server really does.
    """
    server = FakeEmbyServer()
    silent = FakePushConnection()
    talkative = FakePushConnection()
    talkative.deliver(json.dumps(load_emby_fixture("push_library_changed")))
    adapter = _push_adapter(server, FakePushConnector([silent, talkative]))
    try:
        probe = await adapter.probe_push(timeout_seconds=0.05)
        assert probe.upgraded is True
        assert probe.delivering is False
        assert probe.events == ()
        assert probe.detail is None

        probe = await adapter.probe_push(timeout_seconds=0.05)
        assert probe.upgraded is True
        assert probe.delivering is True
        # Arrival order, deduplicated -- `dict.fromkeys`, not a set, so an
        # operator reads what the channel produced in the order it produced
        # it.
        assert probe.events == (
            SourceEventKind.ITEM_ADDED,
            SourceEventKind.ITEM_UPDATED,
            SourceEventKind.ITEM_REMOVED,
        )
    finally:
        await adapter.aclose()


async def test_probe_push_counts_a_message_that_maps_to_no_event() -> None:
    """`delivering=True` with `events=()` is the common case on an idle library.

    Emby's periodic `Sessions` frame maps to no event and is what keeps the channel
    alive. A probe that reported delivery from its own event list would call a healthy
    idle source dead.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    adapter = _push_adapter(server, FakePushConnector([connection]))
    try:
        probe = await adapter.probe_push(timeout_seconds=0.05)
        assert (probe.upgraded, probe.delivering, probe.events) == (True, True, ())
    finally:
        await adapter.aclose()


async def test_probe_push_reports_a_failed_upgrade_rather_than_raising() -> None:
    """The probe returns rather than raises, like `verify()`.

    Its callers are an operator's diagnostic and a status screen, and both exist to
    render a failure rather than handle one.
    """
    server = FakeEmbyServer()
    connector = FakePushConnector()
    connector.fail_next("no route to host")
    adapter = _push_adapter(server, connector)
    try:
        probe = await adapter.probe_push(timeout_seconds=0.05)
        assert probe.upgraded is False
        assert probe.delivering is False
        assert probe.detail is not None
        assert "no route to host" in probe.detail
        assert "session-token" not in probe.detail
        assert "api_key" not in probe.detail
    finally:
        await adapter.aclose()


async def test_probe_push_reports_a_channel_that_went_stale_as_upgraded() -> None:
    """A channel that opened and then went silent still reports `upgraded=True`.

    Past `stale_after` its iterator raises `PortUnavailable`, which arrives at the
    probe's `except UsherPortError` arm. Reporting `upgraded=False` there would hide
    that the handshake succeeded, and "the upgrade failed" and "the upgrade worked and
    nothing came" are different problems with different fixes.
    """
    server = FakeEmbyServer()
    connection = FakePushConnection()
    connection.stall()
    # Open at 0, first watchdog tick at 100, ceiling 90.
    ticks = iter([0.0, 100.0, 200.0])
    adapter = _push_adapter(
        server, FakePushConnector([connection]), clock=lambda: next(ticks), stale_after=90.0
    )
    try:
        probe = await adapter.probe_push(timeout_seconds=BOUND)
        assert probe.upgraded is True
        assert probe.delivering is False
        assert probe.detail is not None
        assert "delivered no message" in probe.detail
        assert "api_key" not in probe.detail
    finally:
        await adapter.aclose()


async def test_aclose_closes_a_client_it_created_and_leaves_an_injected_one() -> None:
    """An injected client stays the caller's, and `aclose` does not close it.

    `EmbyAdapter` normally makes its own and owns that one. Closing someone else's
    client out from under them is the mistake the bulk adapters' no-op `aclose` avoids.
    """
    owned = EmbyAdapter(SOURCE, CREDENTIALS)
    await owned.aclose()
    assert owned._client.is_closed is True

    server = FakeEmbyServer()
    injected = httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=injected)
    await adapter.aclose()
    assert injected.is_closed is False
    await injected.aclose()


async def test_aclose_is_idempotent_for_both_ownership_shapes() -> None:
    """`aclose` is idempotent, because a shutdown path and a delete path can both reach it.

    `DELETE /admin/sources/{id}` racing process shutdown is exactly that. Honest about
    its own strength: `httpx.AsyncClient.aclose()` is itself idempotent, so deleting the
    `if self._closed: return` guard changes nothing observable today. It guards the
    teardown that a second call would break once a socket is closed here too.
    """
    owned = EmbyAdapter(SOURCE, CREDENTIALS)
    await owned.aclose()
    await owned.aclose()
    assert owned._client.is_closed is True

    server = FakeEmbyServer()
    injected = httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=injected)
    await adapter.aclose()
    await adapter.aclose()
    assert injected.is_closed is False
    with pytest.raises(PortUnavailable):
        await adapter.get_item("movie-0")
    await injected.aclose()


async def test_a_closed_adapter_does_not_reach_the_network_at_all() -> None:
    """`aclose()` must stop the adapter, not just the client it may not own.

    Without a closed-check in `EmbySession.user_id()`, `_fetch` calls it first and
    authenticates successfully against the still-open transport before `request()`'s own
    check raises — the right answer comes out, nothing fails, and a closed adapter has
    quietly minted a fresh Emby session. The assertion is therefore on the absence of
    traffic: a closed adapter authenticates zero times.
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
    """The single-flight re-auth, over a transport that genuinely sleeps.

    Over a plain `httpx.MockTransport` nothing really awaits, so the loop runs one
    gathered call through its own re-auth before starting the next and every other call
    observes a fresh token — which passes with the lock deleted. Asserted on observed
    overlap, so it cannot quietly stop being concurrent, and `_fetch` takes the session
    lock twice per call with a window in between that a single-lock test never reaches.
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
    """A 429 mid-walk reaches the caller as `PortRateLimited` carrying the upstream's hint.

    The retry policy backs off on the hint when there is one.
    """

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


# --- the redacted request path ---------------------------------------------


async def test_every_path_this_adapter_issues_redacts_to_a_route_with_no_identifier() -> None:
    """A failure message must not carry the Emby user id.

    `PortUnavailable(f"{method} {path} failed: …")` would put it into `sync_runs.error`,
    and from there into anything that quotes one.
    """
    seen: list[str] = []
    server = FakeEmbyServer()
    server.add_item(_movie(1), changed_at=T0)
    server.set_watch_state(
        SourceWatchState(external_id="movie-1", position_seconds=0, played=False)
    )

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return server.handle(request)

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(recording), base_url=SOURCE.base_url
        ),
        page_size=2,
    )
    try:
        await adapter.verify()
        [item async for item in adapter.list_items()]
        await adapter.get_item("movie-1")
        [state async for state in adapter.watch_state()]
        await adapter.push_watch_state("movie-1", WatchStateUpdate(position_seconds=1, played=True))
    finally:
        await adapter.aclose()

    # The control. Both identifiers really are on the wire, so the
    # assertions below are about a redaction rather than about an empty
    # recording.
    assert seen, "nothing was recorded; the plant did not land"
    assert any(USER_ID in path for path in seen)
    assert any(quote("movie-1", safe="") in path for path in seen)

    redacted = {redact_path(path) for path in seen}
    for shape in redacted:
        assert USER_ID not in shape
        assert "movie-1" not in shape

    # Pinned as a *set*, so a new route redacting to a shape nobody chose
    # fails here. Every one of these still names its route: an operator can
    # tell the listing from the single item from the write-back.
    assert redacted == {
        "/Users/AuthenticateByName",
        "/System/Info/Public",
        "/System/Info",
        "/Users/{user_id}",
        "/Users/{user_id}/Items",
        "/Users/{user_id}/Items/{item_id}",
        "/Users/{user_id}/Items/{item_id}/UserData",
        "/Users/{user_id}/PlayedItems/{item_id}",
    }


async def test_a_failed_get_item_names_the_route_and_not_the_ids() -> None:
    """`get_item` builds its own message and `decode_json` call rather than using `json_body`.

    It is a second raise site that interpolates a path, so it needs its own case.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        return authenticated if authenticated is not None else httpx.Response(502)

    adapter = _on(handler)
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await adapter.get_item("movie-1")
    finally:
        await adapter.aclose()
    message = str(exc_info.value)
    assert "/Users/{user_id}/Items/{item_id}" in message
    assert USER_ID not in message
    assert "movie-1" not in message
