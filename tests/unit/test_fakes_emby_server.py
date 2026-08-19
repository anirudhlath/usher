# tests/unit/test_fakes_emby_server.py
"""`FakeEmbyServer`'s own fidelity: what it claims to model, it models.

A test double with tests of its own, deliberately. The source-adapter
contract runs against the real `EmbyAdapter` through this file, so every
place it quietly diverges from Emby is a place a *wrong adapter* passes
forty assertions. Two such divergences shipped and are pinned here: a
position write that rebuilt the watch state from scratch and silently
discarded the play count and last-played date Emby preserves, and a
renderer that left its own fixture's `SeriesId`/`ParentIndexNumber`/
`IndexNumber` showing through for an item seeded without them -- so a
contract test could assert on an episode number the harness never gave it.

A third is the listing-versus-item `UserData` split M4 added: Emby's
listing route reports `PlayCount: 0` and omits `LastPlayedDate` for an item
whose single-item route carries both, and until M4 this fake rendered the
same block for both routes. Pinned here directly rather than only through
the adapter, because mutation showed the fake's half of that drift is
invisible to every other test in the suite -- a correct adapter discards
those fields whatever the fake supplies, so nothing objects.

A fourth divergence this file could never have caught is why M3's live run
exists: the *route* itself was wrong. `POST
/Users/{user}/PlayingItems/{item}/Progress` answers 400 on a real Emby
4.9.5.0, and a fake that implemented the adapter's own guess agreed with it
perfectly. The write routes below are now transcribed from that server, 400
included.

Driven through `EmbySession` rather than by calling the fake's private
renderers: that is the path the adapter takes, so these assertions cover
the routing and the token gate as well as the rendering. It is not driven
through `EmbyAdapter`, so a bug in the adapter cannot make a bug in the
fake look like correct behaviour.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from tests.fakes.emby_fixtures import load_emby_fixture
from tests.fakes.emby_server import USER_ID, FakeEmbyServer
from usher.adapters.emby.mapping import TICKS_PER_SECOND, to_source_item, to_watch_state
from usher.adapters.emby.push import to_source_events
from usher.adapters.emby.session import EmbySession
from usher.domain.enums import HdrFormat
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortRateLimited, PortUnavailable
from usher.ports.source import SourceEventKind, SourceItem, SourceItemKind, SourceWatchState

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
# Microseconds on purpose: `added_at` and `last_played_at` both carry them,
# and the renderer used to hardcode `.0000000`, truncating both silently.
ADDED_AT = datetime(2024, 3, 1, 18, 22, 11, 123_456, tzinfo=UTC)
LAST_PLAYED = datetime(2026, 7, 20, 21, 0, 0, 654_321, tzinfo=UTC)

MOVIE = SourceItem(
    external_id="movie-1",
    name="Example Movie",
    kind=SourceItemKind.MOVIE,
    year=2021,
    provider_ids={"tmdb": "90000100", "imdb": "tt99000100"},
    container="mkv",
    video_codec="hevc",
    audio_codec="truehd",
    width=3840,
    height=2160,
    audio_channels=8,
    file_size_bytes=68_719_476_736,
    runtime_seconds=9360,
    added_at=ADDED_AT,
)


class _Driver:
    """One authenticated session against one `FakeEmbyServer`."""

    def __init__(self) -> None:
        self.server = FakeEmbyServer()
        self.client = httpx.AsyncClient(
            transport=self.server.transport(), base_url="https://emby.invalid"
        )
        self.session = EmbySession(
            self.client,
            SourceCredentials(
                username=self.server.username, password=SecretStr(self.server.password)
            ),
            source_name="Living Room Emby",
            device_id="9d1f0b6c-0000-7000-8000-000000000001",
            app_version="0.1.0",
        )

    async def payload(self, external_id: str) -> dict[str, Any]:
        return await self.session.json_body(
            "GET", f"/Users/{USER_ID}/Items/{external_id}", op="get_item"
        )

    async def write_user_data(
        self, external_id: str, position_seconds: int, *, played: bool | None = None
    ) -> None:
        payload: dict[str, object] = {"PlaybackPositionTicks": position_seconds * TICKS_PER_SECOND}
        if played is not None:
            payload["Played"] = played
        await self.session.ok(
            "POST",
            f"/Users/{USER_ID}/Items/{external_id}/UserData",
            payload=payload,
            op="push_progress",
        )

    async def report_progress(self, external_id: str, position_seconds: int) -> None:
        """The route the adapter used to use, kept only so a test can pin
        that the fake rejects it the way the live server does."""
        await self.session.ok(
            "POST",
            f"/Users/{USER_ID}/PlayingItems/{external_id}/Progress",
            params={"PositionTicks": str(position_seconds * TICKS_PER_SECOND)},
            op="push_progress",
        )

    async def listed(self, external_id: str) -> dict[str, Any]:
        """The same item as the **listing** route renders it.

        A second method rather than a flag on `payload` above, because the
        two routes are two different observations: Emby 4.9.5.0's listing
        carries a `UserData` block that is *partially* wrong (correct
        position and played flag, a `PlayCount` of 0, no `LastPlayedDate`),
        and a driver that could only ask one of them could not state that.
        """
        body = await self.session.json_body(
            "GET", f"/Users/{USER_ID}/Items", params={"Recursive": "true"}, op="list"
        )
        items = body["Items"]
        assert isinstance(items, list)
        for item in items:
            if isinstance(item, dict) and item.get("Id") == external_id:
                return item
        raise AssertionError(f"{external_id} was not in the listing")

    async def mark_played(self, external_id: str, *, played: bool) -> None:
        await self.session.ok(
            "POST" if played else "DELETE",
            f"/Users/{USER_ID}/PlayedItems/{external_id}",
            op="push_played",
        )

    async def aclose(self) -> None:
        await self.session.aclose()
        await self.client.aclose()


@pytest_asyncio.fixture
async def driver() -> AsyncIterator[_Driver]:
    driver = _Driver()
    try:
        yield driver
    finally:
        await driver.aclose()


# --- the round-trip the harness contract requires -------------------------


async def test_a_seeded_item_round_trips_every_field_it_was_given(driver: _Driver) -> None:
    """`SourceHarness.given_item`: "It must round-trip every field it is
    given". Asserted field by field rather than through the handful the
    contract suite happens to check, because a field the fake invents is
    one a contract test can assert on without the harness ever having
    supplied it."""
    driver.server.add_item(MOVIE, T0)
    item = to_source_item(await driver.payload("movie-1"))
    assert item is not None
    assert item.external_id == MOVIE.external_id
    assert item.name == MOVIE.name
    assert item.kind is MOVIE.kind
    assert item.year == MOVIE.year
    assert item.provider_ids == MOVIE.provider_ids
    assert item.container == MOVIE.container
    assert item.video_codec == MOVIE.video_codec
    assert item.audio_codec == MOVIE.audio_codec
    assert (item.width, item.height) == (MOVIE.width, MOVIE.height)
    assert item.audio_channels == MOVIE.audio_channels
    assert item.file_size_bytes == MOVIE.file_size_bytes
    assert item.runtime_seconds == MOVIE.runtime_seconds
    assert item.hdr_format == MOVIE.hdr_format
    assert item.series_external_id is None
    assert item.season_number is None
    assert item.episode_number is None


async def test_added_at_round_trips_to_the_microsecond(driver: _Driver) -> None:
    """The renderer used to emit a hardcoded `.0000000` fraction, so any
    sub-second part of a seeded `added_at` was silently truncated -- a
    widening nothing stated and no assertion could see, because the
    contract only checks that the value is timezone-aware."""
    driver.server.add_item(MOVIE, T0)
    item = to_source_item(await driver.payload("movie-1"))
    assert item is not None
    assert item.added_at == ADDED_AT


async def test_an_episode_seeded_without_a_series_place_reads_back_empty(
    driver: _Driver,
) -> None:
    """The template's own values used to show through: the episode fixture
    carries `SeriesId`/`ParentIndexNumber`/`IndexNumber`, and the renderer
    only overwrote them when the seeded value was not `None`. An episode
    seeded with all three empty read back as season 2, episode 5 of a
    series the harness had never heard of."""
    driver.server.add_item(
        SourceItem(external_id="episode-1", name="Loose Episode", kind=SourceItemKind.EPISODE),
        T0,
    )
    item = to_source_item(await driver.payload("episode-1"))
    assert item is not None
    assert item.kind is SourceItemKind.EPISODE
    assert item.series_external_id is None
    assert item.season_number is None
    assert item.episode_number is None


async def test_dimensions_survive_an_item_with_no_container(driver: _Driver) -> None:
    """A folder item has no media source, so the stream-level `Width` and
    `Height` have nowhere to live -- and Emby carries item-level ones
    anyway. Without them, seeding an item with dimensions and no container
    dropped both without a word."""
    driver.server.add_item(
        SourceItem(
            external_id="series-1",
            name="Example Series",
            kind=SourceItemKind.SERIES,
            width=1920,
            height=1080,
        ),
        T0,
    )
    item = to_source_item(await driver.payload("series-1"))
    assert item is not None
    assert (item.width, item.height) == (1920, 1080)
    assert item.container is None


# --- watch state, in both directions --------------------------------------


async def test_a_seeded_watch_state_round_trips_all_four_facts(driver: _Driver) -> None:
    """`SourceWatchState` carries four facts, and `_user_data` used to
    render two of them. With no `LastPlayedDate` key ever emitted, an
    adapter that dropped `last_played_at` entirely passed every assertion
    the contract makes."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=1840,
            played=False,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    state = to_watch_state(
        await driver.payload("movie-1"), source_user_id=USER_ID, play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


async def test_the_listing_route_omits_the_play_history_the_item_route_carries(
    driver: _Driver,
) -> None:
    """The measurement ADR-0014 rests on, transcribed as an assertion about
    *this fake* rather than about the adapter.

    Verified 2026-07-31 against the live Emby 4.9.5.0 server: for one and
    the same item, `GET /Users/{u}/Items` reported `PlayCount: 0` with no
    `LastPlayedDate` key at all, while `GET /Users/{u}/Items/{id}` reported
    `PlayCount: 2` and a real date. `PlaybackPositionTicks` and `Played`
    were correct in both -- which is what makes the listing's block a
    *partial* lie and therefore dangerous: every field a harness reads back
    looks right.

    Why this test exists rather than only the adapter-level one: mutation
    (M4 plan, Task 3 Step 7) showed that making this fake's listing carry
    real history is **invisible to the whole suite** if the adapter is
    correct, because a correct adapter discards those fields regardless.
    That is the M3 write-back failure's exact shape -- a fake drifting away
    from the measured server with nothing objecting -- and it is only
    caught by pinning the fake against the measurement directly, which is
    what this does. The adapter-level `test_the_walk_reports_absent_play_
    history` catches the *pair* (adapter trusting the listing plus fake
    supplying history); this catches the fake's half on its own.
    """
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=1840,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    listed = (await driver.listed("movie-1"))["UserData"]
    item = (await driver.payload("movie-1"))["UserData"]

    assert listed["PlayCount"] == 0
    assert "LastPlayedDate" not in listed
    assert item["PlayCount"] == 7
    # The literal seven-digit-fraction form, written out rather than built
    # with the renderer's own `_emby_stamp`: a test that reused the helper
    # under test would agree with it however wrong it became.
    assert item["LastPlayedDate"] == "2026-07-20T21:00:00.6543210Z"
    # The half the listing gets right, asserted so "the listing is wrong
    # about everything" is not what this file ends up claiming.
    position = 1840 * TICKS_PER_SECOND
    assert listed["PlaybackPositionTicks"] == position
    assert item["PlaybackPositionTicks"] == position
    assert listed["Played"] is True
    assert item["Played"] is True


async def test_reporting_progress_changes_the_position_and_nothing_else(
    driver: _Driver,
) -> None:
    """`POST .../Items/{item}/UserData` writes a position. Emby does not
    zero `PlayCount` or clear `LastPlayedDate` when it receives one, and a
    fake that did would let an adapter that destroyed a user's play history
    on every resume look correct -- `recorded_watch_state` reads back only
    position and played, so nothing else in the suite can see it."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=100,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    await driver.write_user_data("movie-1", 600, played=True)
    state = to_watch_state(
        await driver.payload("movie-1"), source_user_id=USER_ID, play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.position_seconds == 600
    assert state.played is True
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


async def test_a_user_data_write_that_omits_played_clears_it(driver: _Driver) -> None:
    """Transcribed from the live server, 2026-07-31: the body deserialises
    into a DTO whose unset fields take their C# defaults, so a write
    carrying only `PlaybackPositionTicks` flipped a played item to unplayed
    -- while leaving `PlayCount` and `LastPlayedDate` intact. The adapter
    names `Played` on every write because of this; the fake models it so
    that dropping the field from the adapter fails somewhere."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=100,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    await driver.write_user_data("movie-1", 600)
    state = to_watch_state(
        await driver.payload("movie-1"), source_user_id=USER_ID, play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.played is False
    assert (state.play_count, state.last_played_at) == (7, LAST_PLAYED)


async def test_the_playing_items_progress_route_is_rejected(driver: _Driver) -> None:
    """`POST /Users/{user}/PlayingItems/{item}/Progress` is session-scoped
    playback reporting and answers 400 `"Value cannot be null. (Parameter
    'key')"` on the live server for every body and parameter set tried.
    Modelled rather than left unrouted so an adapter regressing to it fails
    with Emby's own rejection instead of a 404 that reads like a gap here."""
    driver.server.add_item(MOVIE, T0)
    with pytest.raises(PortUnavailable, match="400"):
        await driver.report_progress("movie-1", 600)


async def test_marking_played_clears_the_position_and_keeps_the_history(
    driver: _Driver,
) -> None:
    """Emby clears the resume position when an item is marked played --
    which is why the adapter writes the position first and the played flag
    last -- and it does not forget when the item was last watched. Verified
    live: a 300-second position really did read back as 0 after this call.

    `PlayCount` stays where it is rather than incrementing, also verified:
    an already-counted item marked played again came back at 1, not 2. That
    is what makes the adapter's retry after a partial failure idempotent."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=1840,
            played=False,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    await driver.mark_played("movie-1", played=True)
    state = to_watch_state(
        await driver.payload("movie-1"), source_user_id=USER_ID, play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.position_seconds == 0
    assert state.played is True
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


async def test_unmarking_played_destroys_the_position_and_the_history(driver: _Driver) -> None:
    """`DELETE /Users/{user}/PlayedItems/{item}` is destructive well beyond
    its name -- verified live on 2026-07-31: `PlayCount` reset to 0,
    `LastPlayedDate` gone, and a non-zero resume position cleared along with
    them. That is exactly why the adapter reports an item unplayed through a
    `UserData` write instead, and why this fake models the destruction
    rather than the polite behaviour the route's name suggests."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=1840,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    await driver.mark_played("movie-1", played=False)
    state = to_watch_state(
        await driver.payload("movie-1"), source_user_id=USER_ID, play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.position_seconds == 0
    assert state.played is False
    assert state.play_count == 0
    assert state.last_played_at is None


async def test_an_untouched_item_reports_a_zero_state_with_no_last_played_date(
    driver: _Driver,
) -> None:
    """Emby omits `LastPlayedDate` for an item nobody has watched rather
    than nulling it, and the zero state is emitted rather than the whole
    `UserData` block being absent -- the difference the port's
    `watch_state` docstring turns on."""
    driver.server.add_item(MOVIE, T0)
    payload = await driver.payload("movie-1")
    assert "LastPlayedDate" not in payload["UserData"]
    state = to_watch_state(payload, source_user_id=USER_ID, play_history_is_trustworthy=True)
    assert state is not None
    assert state.position_seconds == 0
    assert state.played is False
    assert state.play_count == 0
    assert state.last_played_at is None


async def test_writes_to_an_unknown_item_are_not_found(driver: _Driver) -> None:
    """A file deleted between a walk and its write-back. Answering 200 for
    an id the server has never seen would let a write-back to the wrong id
    look successful forever."""
    with pytest.raises(PortUnavailable, match="404"):
        await driver.write_user_data("never-existed", 600, played=False)
    with pytest.raises(PortUnavailable, match="404"):
        await driver.mark_played("never-existed", played=True)


@pytest.mark.parametrize("hdr_format", [None, *HdrFormat])
async def test_every_hdr_format_survives_the_wire(
    driver: _Driver, hdr_format: HdrFormat | None
) -> None:
    """`_HDR_WIRE` renders each canonical format back into the
    `VideoRange`/`VideoRangeType` pair a real file of that kind carries, so
    the contract's HDR assertion is answered by a token the mapper has to
    translate rather than by a value handed straight back. Parametrised
    over the whole enum: only Dolby Vision is exercised by the contract
    suite, so a wrong rendering for HLG or HDR10 would sit here unseen."""
    driver.server.add_item(replace(MOVIE, hdr_format=hdr_format), T0)
    item = to_source_item(await driver.payload("movie-1"))
    assert item is not None
    assert item.hdr_format is hdr_format


# --- the ordering it supplies, and the ordering it refuses to supply ------


async def test_the_listing_honours_the_sort_fields_it_is_given(driver: _Driver) -> None:
    """`SortBy=SortName` is obeyed, so the fake is not merely returning
    insertion order and getting lucky. Seeded out of order on purpose."""
    for name in ("Charlie", "Alpha", "Bravo"):
        driver.server.add_item(
            replace(MOVIE, external_id=name.lower(), name=name), datetime(2026, 1, 1, tzinfo=UTC)
        )
    body = await driver.session.json_body(
        "GET",
        f"/Users/{USER_ID}/Items",
        params={"SortBy": "SortName", "SortOrder": "Ascending", "Limit": "10"},
        op="list",
    )
    assert [entry["Name"] for entry in body["Items"]] == ["Alpha", "Bravo", "Charlie"]


async def test_the_listing_supplies_no_tiebreak_it_was_not_asked_for(driver: _Driver) -> None:
    """The divergence that hid a real paging bug for a whole task.

    This fake used to sort by `(changed_at, external_id)` regardless of
    what the request asked for -- a *total* order the adapter never
    requested from the real server. Under it, `StartIndex` paging over
    `SortBy=DateCreated` alone looked perfectly stable, while against a
    server free to break `DateCreated` ties however it liked it would
    reshuffle the window under its own cursor and drop items.

    So the rule is now the honest one: items a request gave the server no
    way to tell apart come back in an order that is allowed to change
    between requests. Asserted as an inequality between two identical
    requests, which is the only shape that can catch a tiebreak being
    quietly reintroduced.
    """
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(4):
        driver.server.add_item(
            replace(MOVIE, external_id=f"tied-{index}", name="Same Name", added_at=stamp), stamp
        )

    async def ids() -> list[str]:
        body = await driver.session.json_body(
            "GET",
            f"/Users/{USER_ID}/Items",
            params={"SortBy": "DateCreated", "Limit": "10"},
            op="list",
        )
        return [entry["Id"] for entry in body["Items"]]

    first, second = await ids(), await ids()
    assert sorted(first) == sorted(second) == [f"tied-{index}" for index in range(4)]
    assert first != second


# --- the gates the fake keeps, which are only worth having if they fire ---


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer some-token",
        'MediaBrowser Client="SomethingElse", Device="d", DeviceId="i", Version="1"',
        'MediaBrowser Client="Usher", DeviceId="i", Version="1"',
        'MediaBrowser Client="Usher", Device="d", DeviceId="", Version="1"',
        'MediaBrowser Client="Usher", Device="", DeviceId="i", Version="1"',
        'MediaBrowser Client="Usher", Device="d", DeviceId="i", Version=""',
        'MediaBrowser Client="Usher", Device="d", DeviceId="i"',
    ],
)
def test_a_request_without_a_well_formed_identity_is_refused(authorization: str | None) -> None:
    """The gate itself, on a route that is not the authentication one --
    the whole point being that the identity rides on *every* request.
    Checked directly rather than through a session, because a correct
    `EmbySession` cannot produce any of these headers."""
    server = FakeEmbyServer()
    headers = {} if authorization is None else {"Authorization": authorization}
    response = server.handle(
        httpx.Request("GET", "https://emby.invalid/System/Info/Public", headers=headers)
    )
    assert response.status_code == 400


def test_the_public_route_refuses_a_session_token() -> None:
    """Stricter than the real Emby, on purpose, and therefore worth a test
    of its own: this is the guard that makes `verify()`'s
    unreachable-versus-bad-credentials split checkable at all. Without a
    token the same request is a 200."""
    server = FakeEmbyServer()
    identity = 'MediaBrowser Client="Usher", Device="d", DeviceId="i", Version="1"'
    url = "https://emby.invalid/System/Info/Public"
    with_token = server.handle(
        httpx.Request("GET", url, headers={"Authorization": identity, "X-Emby-Token": "t"})
    )
    without = server.handle(httpx.Request("GET", url, headers={"Authorization": identity}))
    assert with_token.status_code == 400
    assert without.status_code == 200


@pytest.mark.parametrize("retry_after", ["120", "Wed, 21 Oct 2026 07:28:00 GMT", None])
def test_an_armed_rate_limit_answers_one_request_for_one_path(retry_after: str | None) -> None:
    """`rate_limit` is scoped to its path and consumed by one firing, and both
    halves are load-bearing rather than tidy.

    A limit that fired for every path would land on `AuthenticateByName`
    instead of on the read a case armed -- `EmbySession` authenticates before
    it reads anything -- and a limit that fired forever would make
    `tests/integration/test_rate_limited_end_to_end.py`'s re-arm between its
    probe and its worker run a no-op nobody could see. The three parameters are
    RFC 9110's two forms and its absence: `usher.adapters.http.
    retry_after_seconds` reaches the date one only after `float(value)` has
    raised, and answers `None` for a 429 that carried no header at all.
    """
    server = FakeEmbyServer()
    identity = 'MediaBrowser Client="Usher", Device="d", DeviceId="i", Version="1"'

    def send(path: str) -> httpx.Response:
        url = f"https://emby.invalid{path}"
        return server.handle(httpx.Request("GET", url, headers={"Authorization": identity}))

    server.rate_limit("/System/Info/Public", retry_after=retry_after)
    assert send("/Users/x/Items/y").status_code != 429, "an armed limit is not a global switch"
    fired = send("/System/Info/Public")
    assert fired.status_code == 429, "the other path's request consumed this one's arming"
    # `.get` rather than a branch: the absent header and the two present forms
    # are one assertion, and `None` is what the absence has to read as.
    assert fired.headers.get("Retry-After") == retry_after
    assert send("/System/Info/Public").status_code == 200, "one arming, one firing"


def test_a_refused_request_does_not_consume_an_armed_rate_limit() -> None:
    """The identity gate answers *before* the limiter, which is where
    `rate_limit`'s docstring says it sits.

    Without this the placement is prose and the two orderings are
    indistinguishable: a limiter in front of the gate answers 429 here, and the
    request that was going to be refused anyway silently spends the arming --
    so the case that armed it goes on to see an ordinary 200 and reads the
    absence of a 429 as an adapter that never asked.
    """
    server = FakeEmbyServer()
    url = "https://emby.invalid/System/Info/Public"
    identity = 'MediaBrowser Client="Usher", Device="d", DeviceId="i", Version="1"'
    server.rate_limit("/System/Info/Public", retry_after="120")

    refused = server.handle(httpx.Request("GET", url))
    assert refused.status_code == 400

    fired = server.handle(httpx.Request("GET", url, headers={"Authorization": identity}))
    assert fired.status_code == 429
    assert fired.headers["Retry-After"] == "120"


async def test_a_rate_limited_handshake_reaches_the_session_as_a_rate_limit(
    driver: _Driver,
) -> None:
    """The limiter sits behind the identity gate and **in front of
    authentication**, and this is the half of that claim nothing else pins.

    The case above pins the first half -- a limiter moved *above* the identity
    gate dies there. The second half was prose until this one existed: moving
    the limiter block *below* the `AuthenticateByName` route arm, which is the
    precise negation of what `rate_limit`'s docstring claims, left the whole
    suite at **5,342 passed / 26 skipped**, because every other arming in this
    repository is on `/System/Info/Public` or an item path and **nothing had
    ever armed the authenticating call**.

    It is also the first exercise `EmbySession._authenticate_locked`'s own 429
    arm has ever had, and that arm is reachable no other way: the 429 check in
    `EmbySession.request` sits after its 401 arm, on a call that by then
    already holds a token. So a limit armed here is the only route to it, and
    the item read never leaving the process is what says the *handshake* was
    what got limited rather than the read behind it.
    """
    driver.server.add_item(MOVIE, T0)
    driver.server.rate_limit("/Users/AuthenticateByName", retry_after="120")

    with pytest.raises(PortRateLimited) as caught:
        await driver.payload("movie-1")

    assert caught.value.retry_after == 120.0
    assert "POST /Users/AuthenticateByName" in driver.server.requests
    assert f"GET /Users/{USER_ID}/Items/movie-1" not in driver.server.requests, (
        "the read went out, so the 429 the session translated was not the handshake's"
    )


async def test_an_unrouted_path_is_a_404_not_a_cheerful_200(driver: _Driver) -> None:
    """A fake that answered every unknown path with a 200 would let an
    adapter calling a route this server has never heard of look correct --
    which is the residual risk the fake carries anyway (nothing here knows
    Emby's real routes), so it must at least not be widened by the
    catch-all."""
    with pytest.raises(PortUnavailable, match="404"):
        await driver.session.json_body("GET", "/Users/x/NoSuchThing", op="probe")


# --- the push frames, and where their provenance runs out ------------------
#
# These four are the *only* independent check on this file's push side. The
# six contract push cases drive the same renderers through the real mapper,
# so the fake sends what the mapper expects and the mapper parses what the
# fake sends -- which is precisely the shape that let M3 ship a write-back
# that had never worked. What narrows it here is that each frame is rendered
# from the committed fixture rather than built inline, so this file cannot
# drift away from the file M5's live verification diffs against a real
# capture; and `tests/unit/test_adapters_emby_push.py` parses those same
# fixtures with no fake server involved. Neither closes the gap. Only the
# live capture does, and `tests/fixtures/emby/README.md` lists what is
# still a guess.


def test_the_push_frames_keep_the_committed_fixtures_shape() -> None:
    """A renderer that invented its own envelope would make the committed
    fixtures decorative -- and those fixtures are the only artefact the
    live capture has to diff against. Asserted on key sets rather than on
    values, because the values here are deliberately the test's."""
    server = FakeEmbyServer()
    for name, frame in (
        ("push_user_data_changed", server.user_data_changed_frame(["90000100"])),
        ("push_library_changed", server.library_changed_frame(added=["90000200"])),
        ("push_sessions", server.sessions_frame()),
    ):
        fixture = load_emby_fixture(name)
        rendered = json.loads(frame)
        assert rendered.keys() == fixture.keys(), name
        assert rendered["MessageType"] == fixture["MessageType"]
        if isinstance(fixture["Data"], dict):
            assert rendered["Data"].keys() == fixture["Data"].keys(), name
    entry = json.loads(server.user_data_changed_frame(["90000100"]))["Data"]["UserDataList"][0]
    template = load_emby_fixture("push_user_data_changed")["Data"]["UserDataList"][0]
    # `LastPlayedDate` is popped for an item with no seeded history -- see
    # the next test, which is what that pop exists for.
    assert entry.keys() <= template.keys()


async def test_a_user_data_changed_frame_carries_the_seeded_state(driver: _Driver) -> None:
    """The arrangement half of every push contract case, checked against
    the real mapper rather than only through the adapter."""
    driver.server.add_item(MOVIE, T0)
    driver.server.set_watch_state(
        SourceWatchState(
            external_id="movie-1",
            position_seconds=91,
            played=True,
            play_count=7,
            last_played_at=LAST_PLAYED,
        )
    )
    frame = driver.server.user_data_changed_frame(["movie-1"])
    events = to_source_events(json.loads(frame), source_user_id=USER_ID)
    assert len(events) == 1
    assert events[0].kind is SourceEventKind.WATCH_STATE_CHANGED
    assert events[0].external_ids == ("movie-1",)
    carried = events[0].watch_states[0]
    assert carried.position_seconds == 91
    assert carried.played is True
    # ADR-0014, on the third payload shape: the frame states 7 and the
    # adapter still reports absence, because no run here has parsed a real
    # `UserDataChanged` and a number it guessed would be merged as a fact.
    assert json.loads(frame)["Data"]["UserDataList"][0]["PlayCount"] == 7
    assert carried.play_count is None
    assert carried.last_played_at is None


async def test_a_user_data_changed_frame_leaves_no_template_value_showing_through(
    driver: _Driver,
) -> None:
    """The trap this file already fell into once, for `SeriesId` and
    `IndexNumber`: rendering a field only when it is set leaves the
    fixture's own invented value in place, and a test then asserts happily
    on a fact the harness never supplied. `push_user_data_changed.json`'s
    first entry carries a `LastPlayedDate` and a `PlayCount` of 3.

    `ItemId` is the only identity field asserted here, because it is the
    only one a real entry carries: M5's live run found **no `Key`** on any
    `UserDataList` entry, so the fixture and this renderer both stopped
    inventing one."""
    driver.server.add_item(MOVIE, T0)
    template = load_emby_fixture("push_user_data_changed")["Data"]["UserDataList"][0]
    assert "LastPlayedDate" in template
    assert template["PlayCount"] == 3
    entry = json.loads(driver.server.user_data_changed_frame(["movie-1"]))["Data"]["UserDataList"][
        0
    ]
    assert "LastPlayedDate" not in entry
    assert entry["PlayCount"] == 0
    assert entry["ItemId"] == "movie-1"
    assert "Key" not in entry


def test_a_library_changed_frame_names_only_the_arrays_it_was_given() -> None:
    """One event per non-empty array is the mapper's rule, so a frame that
    left the fixture's own `ItemsAdded` in place would hand every push case
    an `ITEM_ADDED` nobody arranged."""
    server = FakeEmbyServer()
    fixture = load_emby_fixture("push_library_changed")["Data"]
    assert fixture["ItemsAdded"] and fixture["ItemsRemoved"] and fixture["ItemsUpdated"]
    events = to_source_events(
        json.loads(server.library_changed_frame(updated=["movie-1"])), source_user_id=USER_ID
    )
    assert [(event.kind, event.external_ids) for event in events] == [
        (SourceEventKind.ITEM_UPDATED, ("movie-1",))
    ]
    empty = json.loads(server.library_changed_frame())
    assert empty["Data"]["IsEmpty"] is True
    assert to_source_events(empty, source_user_id=USER_ID) == ()


def test_a_sessions_frame_is_a_message_that_maps_to_no_event() -> None:
    """The property the whole staleness scheme rests on: an idle library's
    channel stays measurably alive because `Sessions` keeps arriving, and
    it produces no event at all. ADR-0004 measured that it arrives
    *periodically* and never at what interval -- so this renders one frame
    on demand and the cadence stays a live-verification question."""
    server = FakeEmbyServer()
    message = json.loads(server.sessions_frame())
    assert message["MessageType"] == "Sessions"
    assert message["Data"][0]["UserId"] == USER_ID
    assert to_source_events(message, source_user_id=USER_ID) == ()
