"""The three send routes over the port fakes and the shipped dependencies, and their DTO."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.house import state
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.players import FakePlayers
from tests.fakes.send_rig import (
    DEADLINE_SECONDS,
    PLAYER,
    FakeTime,
    ParkingPlayers,
    Recording,
    settle_parked,
    two_screen_house,
)
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_default_user_id,
    get_episode_repository,
    get_media_item_repository,
    get_playback_service,
    get_players,
    get_send_dispatcher,
    get_ticket_minter,
    get_title_repository,
    get_watch_state_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.api.dto.sends import SendResponse
from usher.api.errors import RETRY_AFTER_SECONDS
from usher.config import Settings
from usher.domain.enums import HdrFormat, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.screens import project
from usher.domain.sends import Send, SendStage, StageMark
from usher.domain.source import MediaItem
from usher.domain.title import Title
from usher.domain.ways import Way, rank
from usher.ports.events import ClientEventKind
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import wrap_deep_link
from usher.services.playback import PlaybackService
from usher.services.send import SendDispatcher, SendOrder

SOURCE = uuid.UUID("01900000-0000-7000-8000-000000000002")
USER = uuid.UUID("01900000-0000-7000-8000-0000000000a1")
SOURCE_URL = "https://source.usher.test/Videos/99/stream.mkv?api_key=SECRETKEY"
TICKET = "https://usher.test/stream/TICKET/Name"
INFUSE_APP = "com.firecore.infuse"
SEEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
MOVIE = Title(
    kind=TitleKind.MOVIE,
    name="Example Movie",
    sort_name="Example Movie",
    year=2001,
    tmdb_id=90000101,
    imdb_id="tt99000101",
)
SERIES = Title(kind=TitleKind.SERIES, name="Example Series", sort_name="Example Series", year=2002)
SEASON = Season(title_id=SERIES.id, season_number=1)
EPISODE = Episode(
    title_id=SERIES.id,
    season_id=SEASON.id,
    season_number=1,
    episode_number=3,
    name="Example Episode",
)

# Every key a body carries; none of them names a URL, a link or a ticket.
_BODY_KEYS = {
    "send_id",
    "title_id",
    "episode_id",
    "way_id",
    "screen",
    "screen_name",
    "entity_id",
    "recipe",
    "stage",
    "failed_stage",
    "detail",
    "stages",
}


class _Playback(PlaybackService):
    """The only part of `PlaybackService` a send asks: a copy's source URL, or none."""

    def __init__(self, url: str | None) -> None:
        self.url = url
        self.asked = 0

    async def direct_url(self, copy: MediaItem) -> str | None:
        self.asked += 1
        return self.url


@dataclass
class Rig:
    app: FastAPI
    players: ParkingPlayers
    dispatcher: SendDispatcher
    media_items: FakeMediaItemRepository
    playback: _Playback
    minted: list[tuple[str, str]] = field(default_factory=list)
    client: httpx.AsyncClient = field(init=False)

    async def add_copy(
        self,
        *,
        title: Title = MOVIE,
        episode: Episode | None = None,
        container: str = "mkv",
        video_codec: str = "hevc",
    ) -> None:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=f"copy-{container}-{video_codec}",
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

    async def way(self, *, recipe: str = "apple_tv.infuse", episode: Episode | None = None) -> Way:
        """The way `/ways` would show on the Living Room, read from the stored copies."""
        if episode is None:
            copies = await self.media_items.list_for_title(MOVIE.id)
        else:
            copies = await self.media_items.list_for_episode(episode.id)
        ways = rank(project(self.players.snapshot()).screens, copies)
        return next(
            one for one in ways if one.recipe.key == recipe and one.screen.area_id == "living_room"
        )

    def send_title(self, way_id: uuid.UUID, **body: Any) -> Awaitable[httpx.Response]:
        return self.client.post(f"/titles/{MOVIE.id}/send", json={"way": str(way_id), **body})

    def netflix_playing(self) -> None:
        reading = state(
            PLAYER,
            "playing",
            friendly_name="Living Room Apple TV",
            app_id="com.netflix.Netflix",
            app_name="Netflix",
            media_title="Example Episode",
        )
        self.players.publish(self.players.snapshot().with_state(reading))

    def infuse(self, value: str) -> None:
        reading = state(
            PLAYER,
            value,
            friendly_name="Living Room Apple TV",
            app_id=INFUSE_APP,
            app_name="Infuse",
            media_duration_seconds=7_200.0,
        )
        self.players.publish(self.players.snapshot().with_state(reading))

    def nothing_started(self) -> None:
        assert self.players.calls == []
        assert self.minted == []
        assert self.playback.asked == 0


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    players = ParkingPlayers(two_screen_house())
    clock = FakeTime()
    dispatcher = SendDispatcher(players, Recording(), monotonic=clock.monotonic, sleep=clock.sleep)
    titles, episodes = FakeTitleRepository(), FakeEpisodeRepository()
    await titles.add(MOVIE)
    await titles.add(SERIES)
    await episodes.upsert_seasons([SEASON])
    await episodes.upsert_episodes([EPISODE])
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    made = Rig(
        app=app,
        players=players,
        dispatcher=dispatcher,
        media_items=FakeMediaItemRepository(),
        playback=_Playback(SOURCE_URL),
    )

    def mint(url: str, name: str) -> str:
        made.minted.append((url, name))
        return TICKET

    app.dependency_overrides[get_title_repository] = lambda: titles
    app.dependency_overrides[get_episode_repository] = lambda: episodes
    app.dependency_overrides[get_media_item_repository] = lambda: made.media_items
    app.dependency_overrides[get_watch_state_repository] = lambda: FakeWatchStateRepository()
    app.dependency_overrides[get_playback_service] = lambda: made.playback
    app.dependency_overrides[get_default_user_id] = lambda: USER
    app.dependency_overrides[get_ticket_minter] = lambda: mint
    app.dependency_overrides[get_players] = lambda: players
    app.dependency_overrides[get_send_dispatcher] = lambda: dispatcher
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            made.client = connected
            yield made
    await dispatcher.aclose()


def _problem(response: httpx.Response, status: int, code: str, detail: str) -> httpx.Response:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert (body["code"], body["detail"]) == (code, detail)
    return response


async def _until(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while not check():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.001)


async def test_a_send_is_accepted_and_readable(rig: Rig) -> None:
    await rig.add_copy()
    way = await rig.way()
    answer = await rig.send_title(way.id)
    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert set(body) == _BODY_KEYS
    assert body["stage"] == "waking" and body["failed_stage"] is None
    assert body["screen"] == "living_room" and body["recipe"] == "apple_tv.infuse"
    assert (body["title_id"], body["episode_id"], body["way_id"]) == (
        str(MOVIE.id),
        None,
        str(way.id),
    )
    assert body["screen_name"] == "Living Room" and body["entity_id"] == PLAYER
    assert [mark["stage"] for mark in body["stages"]] == ["waking"]
    read = await rig.client.get(f"/sends/{body['send_id']}")
    assert read.status_code == 200, read.text
    assert read.json()["send_id"] == body["send_id"]
    assert read.json()["stages"][0] == body["stages"][0]


@dataclass
class _Refusal:
    path: str
    body: dict[str, Any]
    status: int
    code: str
    detail: str


Arrange = Callable[[Rig], Awaitable[_Refusal]]


async def _unknown_title(rig: Rig) -> _Refusal:
    missing = new_id()
    assert missing != MOVIE.id
    return _Refusal(
        f"/titles/{missing}/send", {"way": str(new_id())}, 404, "not_found", "title not found"
    )


async def _unknown_way(rig: Rig) -> _Refusal:
    await rig.add_copy()
    way = await rig.way()
    unknown = new_id()
    assert unknown != way.id
    return _Refusal(
        f"/titles/{MOVIE.id}/send",
        {"way": str(unknown)},
        422,
        "validation_failed",
        "no way has this id",
    )


async def _unknown_episode(rig: Rig) -> _Refusal:
    missing = new_id()
    assert missing != EPISODE.id
    return _Refusal(
        f"/episodes/{missing}/send", {"way": str(new_id())}, 404, "not_found", "episode not found"
    )


async def _mkv_over_airplay(rig: Rig) -> _Refusal:
    await rig.add_copy(container="mkv")
    way = await rig.way(recipe="apple_tv.airplay")
    assert way.refusal == "MKV OVER AIRPLAY"  # the premise
    return _Refusal(
        f"/titles/{MOVIE.id}/send",
        {"way": str(way.id)},
        409,
        "way_not_playable",
        "CAN'T · MKV OVER AIRPLAY",
    )


async def _mp4_over_airplay(rig: Rig) -> _Refusal:
    await rig.add_copy(container="mp4")
    way = await rig.way(recipe="apple_tv.airplay")
    assert way.playable and not way.recipe.proven  # the premises
    return _Refusal(
        f"/titles/{MOVIE.id}/send",
        {"way": str(way.id)},
        409,
        "way_not_playable",
        "CAN'T SEND · AIRPLAY IS NOT PROVEN",
    )


async def _netflix_playing(rig: Rig) -> _Refusal:
    await rig.add_copy()
    way = await rig.way()
    assert way.playable and way.recipe.proven  # the premises
    rig.netflix_playing()
    return _Refusal(
        f"/titles/{MOVIE.id}/send",
        {"way": str(way.id)},
        409,
        "screen_busy",
        "NETFLIX · EXAMPLE EPISODE",
    )


async def _source_down(rig: Rig) -> _Refusal:
    await rig.add_copy()
    way = await rig.way()
    rig.playback.url = None
    return _Refusal(
        f"/titles/{MOVIE.id}/send",
        {"way": str(way.id)},
        503,
        "source_unavailable",
        "could not reach the source holding this item",
    )


@pytest.mark.parametrize(
    "arrange",
    [
        _unknown_title,
        _unknown_episode,
        _unknown_way,
        _mkv_over_airplay,
        _mp4_over_airplay,
        _netflix_playing,
        _source_down,
    ],
)
async def test_a_send_that_is_not_accepted_says_why(rig: Rig, arrange: Arrange) -> None:
    refusal = await arrange(rig)
    answer = await rig.client.post(refusal.path, json=refusal.body)
    _problem(answer, refusal.status, refusal.code, refusal.detail)
    assert rig.players.calls == [] and rig.minted == []  # no write, no ticket
    if refusal.code != "source_unavailable":
        assert rig.playback.asked == 0  # the source is asked last


async def test_a_body_that_is_not_a_way_is_rejected(rig: Rig) -> None:
    for body in ({}, {"way": "not-an-id"}, {"way": str(new_id()), "take_over": "perhaps"}):
        answer = await rig.client.post(f"/titles/{MOVIE.id}/send", json=body)
        assert answer.status_code == 422, answer.text
        assert answer.json()["code"] == "validation_failed"
    rig.nothing_started()


async def test_players_off_is_503_with_no_retry_after_and_an_unread_house_has_one(
    rig: Rig,
) -> None:
    await rig.add_copy()
    way = await rig.way()
    posts = (f"/titles/{MOVIE.id}/send", f"/episodes/{EPISODE.id}/send")
    # Unread: Home Assistant is expected back.
    rig.players.drop()
    for path in posts:
        answer = await rig.client.post(path, json={"way": str(way.id)})
        assert answer.status_code == 503, answer.text
        assert answer.json()["code"] == "players_unavailable"
        assert answer.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    rig.players.publish(two_screen_house())
    # Off: no players and no dispatcher, and asking again changes nothing.
    rig.app.dependency_overrides[get_players] = lambda: None
    rig.app.dependency_overrides[get_send_dispatcher] = lambda: None
    for path in posts:
        answer = await rig.client.post(path, json={"way": str(way.id)})
        assert answer.status_code == 503, answer.text
        assert answer.json()["code"] == "players_unavailable"
        assert "retry-after" not in answer.headers
    read = await rig.client.get(f"/sends/{new_id()}")
    assert read.status_code == 503, read.text
    assert read.json()["code"] == "players_unavailable"
    assert "retry-after" not in read.headers
    rig.nothing_started()


async def test_take_over_plays_over_what_is_playing(rig: Rig) -> None:
    await rig.add_copy()
    way = await rig.way()
    rig.netflix_playing()
    refused = await rig.send_title(way.id)  # the premise: without it, refused
    _problem(refused, 409, "screen_busy", "NETFLIX · EXAMPLE EPISODE")
    answer = await rig.send_title(way.id, take_over=True)
    assert answer.status_code == 202, answer.text
    assert answer.json()["stage"] == "waking"
    await settle_parked(rig.players)
    assert [call[0] for call in rig.players.calls] == ["wake", "play_url"]


async def test_an_episode_send_is_accepted(rig: Rig) -> None:
    await rig.add_copy(title=SERIES, episode=EPISODE)
    way = await rig.way(episode=EPISODE)
    assert way.file.episode_id == EPISODE.id  # the premise
    answer = await rig.client.post(f"/episodes/{EPISODE.id}/send", json={"way": str(way.id)})
    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert (body["title_id"], body["episode_id"]) == (str(SERIES.id), str(EPISODE.id))
    await settle_parked(rig.players)
    assert [name for _, name in rig.minted] == ["Example Series (2002) - S01E03 - Example Episode"]


async def test_a_send_nobody_made_is_404(rig: Rig) -> None:
    answer = await rig.client.get(f"/sends/{new_id()}")
    _problem(answer, 404, "not_found", "send not found")


async def test_a_finished_send_lists_every_stage_it_passed(rig: Rig) -> None:
    await rig.add_copy()
    way = await rig.way()
    rig.players.on_play = lambda *_: rig.infuse("paused")
    send_id = (await rig.send_title(way.id)).json()["send_id"]
    await settle_parked(rig.players)  # waiting for it to start
    rig.infuse("playing")
    await _until(
        lambda: (found := rig.dispatcher.get(uuid.UUID(send_id))) is not None and found.finished,
        "the send did not finish",
    )
    read = await rig.client.get(f"/sends/{send_id}")
    body = read.json()
    assert [mark["stage"] for mark in body["stages"]] == [
        "waking",
        "opening",
        "starting",
        "playing",
    ]
    assert body["stage"] == "playing" and body["failed_stage"] is None
    assert all(mark["detail"] is None and mark["at"] for mark in body["stages"])


async def test_no_body_carries_the_ticket_the_source_url_or_the_link(rig: Rig) -> None:
    await rig.add_copy()
    way = await rig.way()
    answer = await rig.send_title(way.id)
    await settle_parked(rig.players)
    link = wrap_deep_link(TICKET)
    # The premise: the credentials did reach the places that may hold them.
    assert [call[2] for call in rig.players.calls if call[0] == "play_url"] == [link]
    assert rig.minted == [(SOURCE_URL, "Example Movie (2001) {tmdb-90000101}")]
    read = await rig.client.get(f"/sends/{answer.json()['send_id']}")
    for response in (answer, read):
        assert set(response.json()) == _BODY_KEYS
        for secret in (TICKET, SOURCE_URL, link, "SECRETKEY", "TICKET"):
            assert secret not in response.text


async def test_a_send_that_did_not_take_carries_its_failed_stage_and_detail() -> None:
    at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    waking = Send(
        id=new_id(),
        title_id=MOVIE.id,
        episode_id=None,
        way_id=new_id(),
        area_id="living_room",
        screen_name="Living Room",
        device_name="Living Room Apple TV",
        entity_id=PLAYER,
        recipe_key="apple_tv.infuse",
        marks=(StageMark(stage=SendStage.WAKING, at=at),),
    )
    failed = waking.moved(
        SendStage.DIDNT_TAKE,
        at,
        detail="WAKING · LIVING ROOM APPLE TV STAYED ASLEEP",
        failed_stage=SendStage.WAKING,
    )
    body = SendResponse.of(failed).model_dump(mode="json")
    assert (body["stage"], body["failed_stage"]) == ("didnt_take", "waking")
    assert body["detail"] == "WAKING · LIVING ROOM APPLE TV STAYED ASLEEP"
    assert [(mark["stage"], mark["detail"]) for mark in body["stages"]] == [
        ("waking", None),
        ("didnt_take", "WAKING · LIVING ROOM APPLE TV STAYED ASLEEP"),
    ]


# -- the lifespan --------------------------------------------------------


def _app_with_players(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> tuple[FastAPI, Any]:
    players = FakePlayers(two_screen_house())

    async def house_players(_: Settings) -> tuple[FakePlayers, Callable[[], Awaitable[None]]]:
        async def close() -> None:
            closed.append("players")

        return players, close

    class Recorded(SendDispatcher):
        async def aclose(self) -> None:
            await super().aclose()
            closed.append("sends")

    monkeypatch.setattr("usher.api.app.house_players", house_players)
    monkeypatch.setattr("usher.api.app.SendDispatcher", Recorded)
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    return app, players


async def test_the_lifespan_builds_no_dispatcher_while_players_are_off() -> None:
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    async with LifespanManager(app):
        assert app.state.players is None
        assert app.state.sends is None


async def test_the_lifespan_dispatcher_sends_through_the_players_and_publishes_on_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    app, players = _app_with_players(monkeypatch, closed)
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=SOURCE,
                external_id="copy",
                title_id=MOVIE.id,
                episode_id=None,
                container="mkv",
                video_codec="hevc",
                audio_codec="eac3",
                width=3840,
                height=2160,
                hdr_format=None,
                audio_channels=6,
                file_size_bytes=1,
                runtime_seconds=7_200,
                added_at=None,
                last_seen_at=SEEN,
            )
        ]
    )
    copies = await media_items.list_for_title(MOVIE.id)
    way = next(
        one
        for one in rank(project(players.snapshot()).screens, copies)
        if one.recipe.key == "apple_tv.infuse" and one.screen.area_id == "living_room"
    )
    async with LifespanManager(app):
        assert isinstance(app.state.sends, SendDispatcher)
        async with app.state.events.subscribe() as stream:
            app.state.sends.start(SendOrder(MOVIE.id, None, way, lambda: TICKET))
            async with asyncio.timeout(DEADLINE_SECONDS):
                sent = await anext(stream)
        assert sent.event.kind is ClientEventKind.SEND_STAGE
        assert sent.event.data["stage"] == "waking"
        await _until(lambda: len(players.calls) == 2, "the handover never reached the players")
        assert [call[0] for call in players.calls] == ["wake", "play_url"]
    assert closed == ["sends", "players"]
