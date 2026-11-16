"""`SendService`: a way's id becomes an order, checked in a fixed order, ticket minted late."""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from tests.fakes.house import state
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.send_rig import (
    PLAYER,
    FakeTime,
    ParkingPlayers,
    Recording,
    settle_parked,
    two_screen_house,
)
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import HdrFormat, TitleKind, WatchStateOrigin
from usher.domain.episode import Episode
from usher.domain.house import HouseSnapshot
from usher.domain.ids import new_id
from usher.domain.recipes import RECIPES, Handover
from usher.domain.screens import project
from usher.domain.sends import Send, SendStage
from usher.domain.source import MediaItem
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.domain.ways import Way, rank
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.events import EventPublisher
from usher.ports.ingest import MediaItemUpsert, WatchStateWrite
from usher.ports.players import Players
from usher.ports.source import wrap_deep_link
from usher.services.playback import PlaybackService, episode_name, title_name
from usher.services.screens import PLAYERS_OFF, PlayersUnavailable
from usher.services.send import (
    ScreenBusy,
    SendDispatcher,
    SendOrder,
    SendService,
    SourceUnavailable,
    UnknownWay,
    WayNotPlayable,
    build_link,
)

USER = uuid.UUID("01900000-0000-7000-8000-0000000000a1")
SOURCE = uuid.UUID("01900000-0000-7000-8000-000000000002")
SOURCE_URL = "https://source.usher.test/Videos/99/stream.mkv?api_key=SECRETKEY"
TICKET = "https://usher.test/stream/TICKET/Name"
SEEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

MOVIE = Title(
    kind=TitleKind.MOVIE,
    name="Example Movie",
    sort_name="Example Movie",
    year=2001,
    tmdb_id=90000101,
    imdb_id="tt99000101",
)
SERIES = Title(
    kind=TitleKind.SERIES,
    name="Example Series",
    sort_name="Example Series",
    year=2002,
    tmdb_id=90000102,
    imdb_id="tt99000102",
)
EPISODE = Episode(
    title_id=SERIES.id,
    season_id=new_id(),
    season_number=1,
    episode_number=3,
    name="Example Episode",
)

Settle = Callable[..., Awaitable[None]]


class RecordingPlayback(PlaybackService):
    """The only part of `PlaybackService` a send asks: a copy's source URL, or none."""

    def __init__(self, url: str | None) -> None:
        self.url = url
        self.asked: list[MediaItem] = []

    async def direct_url(self, copy: MediaItem) -> str | None:
        self.asked.append(copy)
        return self.url


class RecordingDispatcher(SendDispatcher):
    """Every order `start` is given, so a refused request is shown to have started nothing."""

    def __init__(
        self,
        players: Players,
        events: EventPublisher,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        super().__init__(players, events, monotonic=monotonic, sleep=sleep)
        self.orders: list[SendOrder] = []

    def start(self, order: SendOrder) -> Send:
        self.orders.append(order)
        return super().start(order)


@dataclass
class Harness:
    players: ParkingPlayers
    events: Recording
    clock: FakeTime
    dispatcher: RecordingDispatcher
    media_items: FakeMediaItemRepository
    watch_states: FakeWatchStateRepository
    playback: RecordingPlayback
    minted: list[tuple[str, str]] = field(default_factory=list)

    def mint(self, url: str, name: str) -> str:
        self.minted.append((url, name))
        return TICKET

    def build(self, *, players: Players | None, dispatcher: SendDispatcher | None) -> SendService:
        return SendService(
            players,
            dispatcher,
            self.media_items,
            self.playback,
            self.watch_states,
            self.mint,
        )

    @property
    def service(self) -> SendService:
        return self.build(players=self.players, dispatcher=self.dispatcher)

    async def add_copy(
        self,
        *,
        title: Title = MOVIE,
        episode: Episode | None = None,
        external_id: str = "copy",
        container: str = "mkv",
        video_codec: str = "hevc",
    ) -> None:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=external_id,
                    title_id=title.id,
                    episode_id=episode.id if episode else None,
                    container=container,
                    video_codec=video_codec,
                    audio_codec="eac3",
                    width=3840,
                    height=2160,
                    hdr_format=HdrFormat.HDR10,
                    audio_channels=6,
                    file_size_bytes=1,
                    runtime_seconds=7_200,
                    added_at=None,
                    last_seen_at=SEEN,
                )
            ]
        )

    async def way(
        self,
        *,
        recipe: str = "apple_tv.infuse",
        area: str = "living_room",
        episode: Episode | None = None,
    ) -> Way:
        """The way `/ways` would have shown, read from the same copies."""
        if episode is None:
            copies = await self.media_items.list_for_title(MOVIE.id)
        else:
            copies = await self.media_items.list_for_episode(episode.id)
        ways = rank(project(self.players.snapshot()).screens, copies)
        return next(one for one in ways if one.recipe.key == recipe and one.screen.area_id == area)

    def assert_nothing_started(self) -> None:
        assert self.dispatcher.orders == []
        assert self.players.calls == []
        assert self.minted == []


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    players = ParkingPlayers(two_screen_house())
    events, clock = Recording(), FakeTime()
    made = Harness(
        players=players,
        events=events,
        clock=clock,
        dispatcher=RecordingDispatcher(
            players, events, monotonic=clock.monotonic, sleep=clock.sleep
        ),
        media_items=FakeMediaItemRepository(),
        watch_states=FakeWatchStateRepository(),
        playback=RecordingPlayback(SOURCE_URL),
    )
    yield made
    await made.dispatcher.aclose()


@pytest.fixture
def settle(harness: Harness) -> Settle:
    async def run(parked: int = 1) -> None:
        await settle_parked(harness.players, parked)

    return run


def netflix_playing(harness: Harness) -> None:
    reading = state(
        PLAYER,
        "playing",
        friendly_name="Living Room Apple TV",
        app_id="com.netflix.Netflix",
        app_name="Netflix",
        media_title="Example Episode",
    )
    harness.players.publish(harness.players.snapshot().with_state(reading))


def watched(*, position: int, played: bool, episode: bool = False) -> WatchStateWrite:
    return WatchStateWrite(
        user_id=USER,
        title_id=None if episode else MOVIE.id,
        episode_id=EPISODE.id if episode else None,
        position_seconds=position,
        played=played,
    )


# -- the link ----------------------------------------------------------


def test_a_resume_point_rides_the_link_and_zero_does_not() -> None:
    ticket = "https://usher.test/stream/TICKET/Name"
    assert build_link(Handover.INFUSE_CALLBACK, ticket, position_seconds=0) == wrap_deep_link(
        ticket
    )
    assert (
        build_link(Handover.INFUSE_CALLBACK, ticket, position_seconds=1234)
        == wrap_deep_link(ticket) + "&position=1234"
    )


def test_every_proven_recipe_has_a_link_to_build() -> None:
    proven = [recipe for recipe in RECIPES if recipe.proven]
    assert proven  # the premise
    for recipe in proven:
        assert build_link(recipe.handover, "https://usher.test/stream/T/N", position_seconds=0)


def test_a_handover_with_no_link_to_build_is_refused_by_name() -> None:
    unbuilt = [recipe.handover for recipe in RECIPES if not recipe.proven]
    assert Handover.AIRPLAY_URL in unbuilt  # the premise
    with pytest.raises(NotImplementedError, match=Handover.AIRPLAY_URL.value):
        build_link(Handover.AIRPLAY_URL, "https://usher.test/stream/T/N", position_seconds=0)


# -- check 1: the house ------------------------------------------------


async def test_players_off_answers_before_the_way_is_looked_up(harness: Harness) -> None:
    service = harness.build(players=None, dispatcher=None)
    with pytest.raises(PlayersUnavailable) as caught:
        await service.send_title(MOVIE, way_id=new_id(), take_over=False, user_id=USER)
    assert caught.value.detail == PLAYERS_OFF
    assert caught.value.retryable is False
    assert harness.playback.asked == []
    harness.assert_nothing_started()


async def test_players_without_a_dispatcher_are_off_too(harness: Harness) -> None:
    service = harness.build(players=harness.players, dispatcher=None)
    with pytest.raises(PlayersUnavailable) as caught:
        await service.send_title(MOVIE, way_id=new_id(), take_over=False, user_id=USER)
    assert caught.value.detail == PLAYERS_OFF
    assert caught.value.retryable is False
    harness.assert_nothing_started()


async def test_a_house_that_cannot_be_read_is_unavailable_in_the_ports_words(
    harness: Harness,
) -> None:
    await harness.add_copy()
    way = await harness.way()
    harness.players.drop()
    with pytest.raises(PlayersUnavailable) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == str(PortUnavailable("the fake house has not been published"))
    assert caught.value.retryable is True
    assert harness.playback.asked == []
    harness.assert_nothing_started()


async def test_a_refused_credential_is_not_retryable(harness: Harness) -> None:
    class Refused(ParkingPlayers):
        def snapshot(self) -> HouseSnapshot:
            raise PortAuthFailed("auth_invalid")

    refused = Refused(two_screen_house())
    service = harness.build(players=refused, dispatcher=harness.dispatcher)
    with pytest.raises(PlayersUnavailable) as caught:
        await service.send_episode(SERIES, EPISODE, way_id=new_id(), take_over=False, user_id=USER)
    assert caught.value.detail == "auth_invalid"
    assert caught.value.retryable is False
    harness.assert_nothing_started()


# -- check 2: the way --------------------------------------------------


async def test_no_way_with_this_id_is_unknown(harness: Harness) -> None:
    await harness.add_copy()
    with pytest.raises(UnknownWay):
        await harness.service.send_title(MOVIE, way_id=new_id(), take_over=False, user_id=USER)
    assert harness.playback.asked == []
    harness.assert_nothing_started()


async def test_a_way_of_another_items_copy_is_unknown_here(harness: Harness) -> None:
    await harness.add_copy()
    await harness.add_copy(title=SERIES, episode=EPISODE, external_id="episode-copy")
    episode_way = await harness.way(episode=EPISODE)
    assert episode_way.file.episode_id == EPISODE.id  # the premise
    with pytest.raises(UnknownWay):
        await harness.service.send_title(
            MOVIE, way_id=episode_way.id, take_over=False, user_id=USER
        )
    harness.assert_nothing_started()


# -- check 3: a refusal ------------------------------------------------


async def test_a_way_with_a_refusal_says_it_cannot_play(harness: Harness) -> None:
    await harness.add_copy(video_codec="av1")
    way = await harness.way()
    assert way.refusal == "AV1 OVER INFUSE"  # the premise
    with pytest.raises(WayNotPlayable) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "CAN'T · AV1 OVER INFUSE"
    assert harness.playback.asked == []
    harness.assert_nothing_started()


# -- check 4: a recipe not yet proven ----------------------------------


async def test_a_recipe_not_proven_cannot_send(harness: Harness) -> None:
    await harness.add_copy(container="mp4")
    way = await harness.way(recipe="apple_tv.airplay")
    assert way.playable
    assert not way.recipe.proven  # the premises
    with pytest.raises(WayNotPlayable) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "CAN'T SEND · AIRPLAY IS NOT PROVEN"
    assert harness.playback.asked == []
    harness.assert_nothing_started()


async def test_a_refusal_is_reported_before_a_recipe_not_proven(harness: Harness) -> None:
    await harness.add_copy(container="mkv")
    way = await harness.way(recipe="apple_tv.airplay")
    assert way.refusal == "MKV OVER AIRPLAY"
    assert not way.recipe.proven  # the premises
    with pytest.raises(WayNotPlayable) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "CAN'T · MKV OVER AIRPLAY"
    harness.assert_nothing_started()


# -- check 5: a busy screen --------------------------------------------


async def test_a_busy_screen_is_taken_over_only_when_asked(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy()
    way = await harness.way()
    netflix_playing(harness)
    with pytest.raises(ScreenBusy) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "NETFLIX · EXAMPLE EPISODE"
    assert harness.playback.asked == []
    harness.assert_nothing_started()
    send = await harness.service.send_title(MOVIE, way_id=way.id, take_over=True, user_id=USER)
    assert send.stage is SendStage.WAKING
    await settle()


async def test_a_recipe_not_proven_is_reported_before_a_busy_screen(harness: Harness) -> None:
    await harness.add_copy(container="mp4")
    way = await harness.way(recipe="apple_tv.airplay")
    netflix_playing(harness)
    with pytest.raises(WayNotPlayable):
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    harness.assert_nothing_started()


async def test_a_busy_screen_is_reported_before_the_source_is_asked(harness: Harness) -> None:
    await harness.add_copy()
    way = await harness.way()
    netflix_playing(harness)
    harness.playback.url = None
    with pytest.raises(ScreenBusy):
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert harness.playback.asked == []
    harness.assert_nothing_started()


# -- check 6: the source -----------------------------------------------


async def test_a_source_with_no_direct_url_is_unavailable(harness: Harness) -> None:
    await harness.add_copy()
    way = await harness.way()
    harness.playback.url = None
    with pytest.raises(SourceUnavailable) as caught:
        await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "could not reach the source holding this item"
    assert harness.playback.asked == [way.file]
    harness.assert_nothing_started()


# -- the ticket --------------------------------------------------------


async def test_the_ticket_is_minted_at_the_handover_and_not_at_the_request(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy()
    way = await harness.way()
    send = await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    assert harness.minted == []  # answered, nothing minted
    await settle()
    assert harness.minted == [(SOURCE_URL, title_name(MOVIE))]
    assert harness.players.calls[-1] == (
        "play_url",
        PLAYER,
        build_link(Handover.INFUSE_CALLBACK, TICKET, position_seconds=0),
    )
    assert harness.dispatcher.get(send.id) is not None


async def test_the_order_names_the_title_the_way_and_the_screen(harness: Harness) -> None:
    await harness.add_copy()
    way = await harness.way()
    send = await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    (order,) = harness.dispatcher.orders
    assert (order.title_id, order.episode_id, order.way) == (MOVIE.id, None, way)
    assert (send.title_id, send.episode_id, send.way_id) == (MOVIE.id, None, way.id)
    assert send.area_id == "living_room"


async def test_no_credential_reaches_a_send_or_an_event(harness: Harness, settle: Settle) -> None:
    await harness.add_copy()
    way = await harness.way()
    send = await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    await settle()
    link = build_link(Handover.INFUSE_CALLBACK, TICKET, position_seconds=0)
    assert harness.players.calls[-1][2] == link  # the premise: the handover happened
    rendered = repr(harness.dispatcher.get(send.id)) + json.dumps(
        [event.data for event in harness.events.events]
    )
    for secret in (SOURCE_URL, "SECRETKEY", TICKET, link):
        assert secret not in rendered


# -- the resume point --------------------------------------------------


@pytest.mark.parametrize(
    ("watch", "suffix"),
    [
        (None, ""),
        (watched(position=1234, played=False), "&position=1234"),
        (watched(position=1234, played=True), ""),
        (watched(position=0, played=False), ""),
    ],
)
async def test_the_resume_point_is_the_users_unless_played(
    harness: Harness, settle: Settle, watch: WatchStateWrite | None, suffix: str
) -> None:
    await harness.add_copy()
    way = await harness.way()
    if watch is not None:
        stored = await harness.watch_states.set_from_client(watch)
        assert (stored.position_seconds, stored.played) == (watch.position_seconds, watch.played)
    await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    await settle()
    assert harness.players.calls[-1][2] == wrap_deep_link(TICKET) + suffix


async def test_another_users_progress_is_not_the_resume_point(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy()
    way = await harness.way()
    other = replace(watched(position=1234, played=False), user_id=new_id())
    await harness.watch_states.set_from_client(other)
    await harness.service.send_title(MOVIE, way_id=way.id, take_over=False, user_id=USER)
    await settle()
    assert harness.players.calls[-1][2] == wrap_deep_link(TICKET)


# -- an episode --------------------------------------------------------


async def test_an_episode_is_scoped_to_its_series_and_named_as_an_episode(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy(title=SERIES, episode=EPISODE, external_id="episode-copy")
    way = await harness.way(episode=EPISODE)
    send = await harness.service.send_episode(
        SERIES, EPISODE, way_id=way.id, take_over=False, user_id=USER
    )
    assert (send.title_id, send.episode_id) == (SERIES.id, EPISODE.id)
    await settle()
    assert harness.minted[0][1] == episode_name(SERIES, EPISODE)


async def test_an_episode_whose_series_is_unknown_is_named_without_it(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy(title=SERIES, episode=EPISODE, external_id="episode-copy")
    way = await harness.way(episode=EPISODE)
    send = await harness.service.send_episode(
        None, EPISODE, way_id=way.id, take_over=False, user_id=USER
    )
    assert send.title_id == EPISODE.title_id
    await settle()
    assert harness.minted[0][1] == episode_name(None, EPISODE)


async def test_an_episode_resumes_from_its_own_progress_and_not_its_series(
    harness: Harness, settle: Settle
) -> None:
    await harness.add_copy(title=SERIES, episode=EPISODE, external_id="episode-copy")
    way = await harness.way(episode=EPISODE)
    series_wide = WatchStateWrite(
        user_id=USER, title_id=SERIES.id, episode_id=None, position_seconds=999, played=False
    )
    own = watched(position=1234, played=False, episode=True)
    for write in (series_wide, own):
        stored: WatchState = await harness.watch_states.set_from_client(write)
        assert stored.origin is WatchStateOrigin.API  # the premise: both are stored
    await harness.service.send_episode(
        SERIES, EPISODE, way_id=way.id, take_over=False, user_id=USER
    )
    await settle()
    assert harness.players.calls[-1][2] == wrap_deep_link(TICKET) + "&position=1234"
