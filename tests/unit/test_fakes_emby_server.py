# tests/unit/test_fakes_emby_server.py
"""`FakeEmbyServer`'s own fidelity: what it claims to model, it models.

A test double with tests of its own, deliberately. The source-adapter
contract runs against the real `EmbyAdapter` through this file, so every
place it quietly diverges from Emby is a place a *wrong adapter* passes
forty assertions. Two such divergences shipped and are pinned here: a
`POST .../Progress` that rebuilt the watch state from scratch and silently
discarded the play count and last-played date Emby preserves, and a
renderer that left its own fixture's `SeriesId`/`ParentIndexNumber`/
`IndexNumber` showing through for an item seeded without them -- so a
contract test could assert on an episode number the harness never gave it.

Driven through `EmbySession` rather than by calling the fake's private
renderers: that is the path the adapter takes, so these assertions cover
the routing and the token gate as well as the rendering. It is not driven
through `EmbyAdapter`, so a bug in the adapter cannot make a bug in the
fake look like correct behaviour.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from tests.fakes.emby_server import USER_ID, FakeEmbyServer
from usher.adapters.emby.mapping import TICKS_PER_SECOND, to_source_item, to_watch_state
from usher.adapters.emby.session import EmbySession
from usher.domain.enums import HdrFormat
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState

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
    provider_ids={"tmdb": "438631", "imdb": "tt1160419"},
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

    async def report_progress(self, external_id: str, position_seconds: int) -> None:
        await self.session.ok(
            "POST",
            f"/Users/{USER_ID}/PlayingItems/{external_id}/Progress",
            params={"PositionTicks": str(position_seconds * TICKS_PER_SECOND)},
            op="push_progress",
        )

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
    state = to_watch_state(await driver.payload("movie-1"), source_user_id=USER_ID)
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


async def test_reporting_progress_changes_the_position_and_nothing_else(
    driver: _Driver,
) -> None:
    """`POST .../Progress` reports a position. Emby does not zero
    `PlayCount` or clear `LastPlayedDate` when it receives one, and a fake
    that did would let an adapter that destroyed a user's play history on
    every resume look correct -- `recorded_watch_state` reads back only
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
    await driver.report_progress("movie-1", 600)
    state = to_watch_state(await driver.payload("movie-1"), source_user_id=USER_ID)
    assert state is not None
    assert state.position_seconds == 600
    assert state.played is True
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


async def test_marking_played_clears_the_position_and_keeps_the_history(
    driver: _Driver,
) -> None:
    """Emby clears the resume position when an item is marked played --
    which is why the adapter writes the position first and the played flag
    last -- and increments the play count. It does not forget when the item
    was last watched."""
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
    state = to_watch_state(await driver.payload("movie-1"), source_user_id=USER_ID)
    assert state is not None
    assert state.position_seconds == 0
    assert state.played is True
    assert state.play_count == 8
    assert state.last_played_at == LAST_PLAYED


async def test_unmarking_played_keeps_the_position_and_the_history(driver: _Driver) -> None:
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
    state = to_watch_state(await driver.payload("movie-1"), source_user_id=USER_ID)
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 7
    assert state.last_played_at == LAST_PLAYED


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
    state = to_watch_state(payload, source_user_id=USER_ID)
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
        await driver.report_progress("never-existed", 600)
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


async def test_an_unrouted_path_is_a_404_not_a_cheerful_200(driver: _Driver) -> None:
    """A fake that answered every unknown path with a 200 would let an
    adapter calling a route this server has never heard of look correct --
    which is the residual risk the fake carries anyway (nothing here knows
    Emby's real routes), so it must at least not be widened by the
    catch-all."""
    with pytest.raises(PortUnavailable, match="404"):
        await driver.session.json_body("GET", "/Users/x/NoSuchThing", op="probe")
