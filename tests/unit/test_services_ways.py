"""`WaysService`: the domain's ranking over a title's or an episode's copies, and the house."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.house import BEDROOM, KITCHEN, LIVING_ROOM, SHIELD_DEVICE, this_house
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.players import FakePlayers
from usher.domain.enums import HdrFormat
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import project
from usher.domain.ways import rank
from usher.ports.ingest import MediaItemUpsert
from usher.services.ways import UnknownScreen, WaysAnswer, WaysService

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TITLE = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
SERIES = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
EPISODE = uuid.UUID("00000000-0000-4000-8000-0000000000dd")
_SEEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _upsert(external_id: str, **fields: Any) -> MediaItemUpsert:
    row: dict[str, Any] = {
        "source_id": SOURCE,
        "external_id": external_id,
        "title_id": TITLE,
        "episode_id": None,
        "container": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "hdr_format": None,
        "audio_channels": None,
        "file_size_bytes": None,
        "runtime_seconds": None,
        "added_at": None,
        "last_seen_at": _SEEN,
    }
    return MediaItemUpsert(**(row | fields))


async def _copies() -> FakeMediaItemRepository:
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many(
        [
            _upsert(
                "film-dv",
                container="mkv",
                video_codec="hevc",
                hdr_format=HdrFormat.DOLBY_VISION,
                audio_codec="truehd",
                audio_channels=8,
                width=3840,
                height=2160,
            ),
            _upsert("film-h264", container="mp4", video_codec="h264", width=1920, height=1080),
            _upsert(
                "episode-hevc",
                title_id=SERIES,
                episode_id=EPISODE,
                container="mkv",
                video_codec="hevc",
                width=1920,
                height=1080,
            ),
        ]
    )
    return media_items


def _two_screens() -> HouseSnapshot:
    """The Apple TV in the Living Room, and the SHIELD placed in the Bedroom."""
    house = this_house(shield=DeviceRole.USE)
    devices = tuple(
        replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
        for one in house.devices
    )
    return house.with_registries(devices=devices)


def _external_ids(answer: WaysAnswer) -> set[str]:
    return {way.file.external_id for way in answer.ways}


async def test_the_ways_are_the_domain_s_ranking_of_every_screen() -> None:
    media_items = await _copies()
    house = _two_screens()
    answer = await WaysService(FakePlayers(house), media_items).for_title(TITLE)
    screens = project(house).screens
    expected = rank(screens, await media_items.list_for_title(TITLE))
    assert {way.screen.area_id for way in expected} == {BEDROOM, LIVING_ROOM}  # premise
    assert answer == WaysAnswer(screens_available=True, screens=screens, ways=expected)


async def test_a_title_s_ways_are_its_own_copies_and_an_episode_s_are_its_own() -> None:
    media_items = await _copies()
    service = WaysService(FakePlayers(this_house()), media_items)
    assert _external_ids(await service.for_title(TITLE)) == {"film-dv", "film-h264"}
    assert _external_ids(await service.for_episode(EPISODE)) == {"episode-hevc"}


async def test_a_named_screen_answers_its_ways_alone() -> None:
    service = WaysService(FakePlayers(_two_screens()), await _copies())
    answer = await service.for_title(TITLE, screen=BEDROOM)
    assert answer.ways  # premise: the Bedroom has ways at all
    assert {way.screen.area_id for way in answer.ways} == {BEDROOM}
    assert [screen.area_id for screen in answer.screens] == [BEDROOM]


@pytest.mark.parametrize("screen", [None, BEDROOM], ids=["every-screen", "named"])
async def test_a_title_with_no_copy_answers_every_screen_it_ranked_on(screen: str | None) -> None:
    house = _two_screens()
    media_items = await _copies()
    assert await media_items.list_for_title(SERIES) == [], "the premise: no copy of its own"
    answer = await WaysService(FakePlayers(house), media_items).for_title(SERIES, screen=screen)
    every = project(house).screens
    assert [one.area_id for one in every] == [BEDROOM, LIVING_ROOM], "the premise: two screens"
    expected = every if screen is None else tuple(one for one in every if one.area_id == screen)
    assert answer == WaysAnswer(screens_available=True, screens=expected, ways=())


@pytest.mark.parametrize("screen", [KITCHEN, "attic"], ids=["area-with-no-player", "no-area"])
async def test_a_screen_that_is_not_a_screen_is_refused(screen: str) -> None:
    service = WaysService(FakePlayers(this_house()), await _copies())
    with pytest.raises(UnknownScreen):
        await service.for_title(TITLE, screen=screen)


@pytest.mark.parametrize("players", [None, FakePlayers()], ids=["off", "unread"])
@pytest.mark.parametrize("screen", [None, LIVING_ROOM], ids=["every-screen", "named"])
async def test_no_house_answers_no_screens_and_no_ways(
    players: FakePlayers | None, screen: str | None
) -> None:
    service = WaysService(players, await _copies())
    assert await service.for_title(TITLE, screen=screen) == WaysAnswer(
        screens_available=False, screens=(), ways=()
    )
