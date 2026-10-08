"""`ScreensService`: the view, and confirm's checks, its writes and its wait."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    BEDROOM,
    LIVING_ROOM,
    MA_DUPLICATE,
    SHIELD,
    SHIELD_DEVICE,
    entity_of,
    this_house,
)
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import project
from usher.ports.errors import PortAuthFailed, PortDataMalformed
from usher.services.screens import (
    PLAYERS_OFF,
    CannotPlace,
    PlayersUnavailable,
    ScreensService,
    UnknownArea,
    UnknownCandidate,
)

_TIMEOUT = 1.0


def _service(house: HouseSnapshot | None) -> tuple[ScreensService, FakePlayers]:
    players = FakePlayers(house)
    return ScreensService(players, confirm_timeout=_TIMEOUT), players


def _with_role(house: HouseSnapshot, entity_id: str, role: DeviceRole) -> HouseSnapshot:
    entities = tuple(
        replace(one, role=role) if one.entity_id == entity_id else one for one in house.entities
    )
    return house.with_registries(entities=entities)


def _placed(house: HouseSnapshot, device_id: str, area_id: str) -> HouseSnapshot:
    devices = tuple(
        replace(one, area_id=area_id) if one.device_id == device_id else one
        for one in house.devices
    )
    return house.with_registries(devices=devices)


def test_the_house_is_the_latest_reading_projected() -> None:
    service, _ = _service(this_house())
    assert service.house() == project(this_house())


def test_players_off_is_unavailable_and_says_so() -> None:
    with pytest.raises(PlayersUnavailable) as failure:
        ScreensService(None, confirm_timeout=_TIMEOUT).house()
    assert failure.value.detail == PLAYERS_OFF


def test_an_unread_house_is_unavailable_in_the_port_s_words() -> None:
    service, _ = _service(None)
    with pytest.raises(PlayersUnavailable) as failure:
        service.house()
    assert failure.value.detail == "the fake house has not been published"


async def test_confirming_use_answers_the_house_after_the_registry_reports_it() -> None:
    service, players = _service(this_house(apple_tv=None))
    assert project(players.snapshot()).screens == ()  # premise: nothing confirmed yet
    view = await service.confirm(APPLE_TV, use=True)
    screen = view.screen(LIVING_ROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [APPLE_TV]
    assert players.writes == [("role", APPLE_TV, "use")]


async def test_confirming_ignore_moves_the_player_to_ignored() -> None:
    service, players = _service(this_house())
    view = await service.confirm(APPLE_TV, use=False)
    assert [player.entity_id for player in view.ignored] == [APPLE_TV]
    assert view.screens == ()
    assert players.writes == [("role", APPLE_TV, "ignore")]


async def test_placing_a_player_writes_its_role_then_its_device_s_area() -> None:
    service, players = _service(this_house())
    view = await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    screen = view.screen(BEDROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [SHIELD]


async def test_confirm_waits_until_both_writes_are_reported() -> None:
    house = this_house()
    service, players = _service(house)
    players.reports_writes = False
    async with asyncio.timeout(_TIMEOUT):
        confirming = asyncio.create_task(service.confirm(SHIELD, use=True, area_id=BEDROOM))
        while len(players.writes) < 2:
            await asyncio.sleep(0)
        role_only = _with_role(house, SHIELD, DeviceRole.USE)
        players.publish(role_only)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not confirming.done(), "confirm answered before the area was reported"
        players.publish(_placed(role_only, SHIELD_DEVICE, BEDROOM))
        view = await confirming
    assert view.screen(BEDROOM) is not None


@pytest.mark.parametrize("entity_id", ["media_player.no_such_player", MA_DUPLICATE])
async def test_a_player_that_is_not_a_candidate_is_refused_before_anything_is_written(
    entity_id: str,
) -> None:
    service, players = _service(this_house())
    assert players.snapshot().entity(MA_DUPLICATE) is not None  # premise: in HA, not a candidate
    with pytest.raises(UnknownCandidate):
        await service.confirm(entity_id, use=True)
    assert players.writes == []


async def test_an_unknown_area_is_refused_before_anything_is_written() -> None:
    service, players = _service(this_house())
    with pytest.raises(UnknownArea):
        await service.confirm(SHIELD, use=True, area_id="attic")
    assert players.writes == []


def _as_probed(house: HouseSnapshot) -> HouseSnapshot:
    return house


def _with_its_own_stale_area(house: HouseSnapshot) -> HouseSnapshot:
    entities = tuple(
        replace(one, area_id="deleted_area") if one.entity_id == SHIELD else one
        for one in house.entities
    )
    return house.with_registries(entities=entities)


@pytest.mark.parametrize(
    ("player", "shape", "reason"),
    [
        (APPLE_TV, _as_probed, "is already in an area"),
        (SHIELD, _with_its_own_stale_area, "carries an area of its own"),
    ],
    ids=["in-an-area", "own-area"],
)
async def test_only_a_player_with_no_area_of_any_kind_is_placed(
    player: str, shape: Callable[[HouseSnapshot], HouseSnapshot], reason: str
) -> None:
    house = shape(this_house(shield=DeviceRole.USE))
    service, players = _service(house)
    assert project(house).candidate(player) is not None  # premise: each is still a candidate
    with pytest.raises(CannotPlace, match=reason):
        await service.confirm(player, use=True, area_id=BEDROOM)
    assert players.writes == []


async def test_an_ignored_player_is_never_placed() -> None:
    service, players = _service(this_house())
    with pytest.raises(CannotPlace) as failure:
        await service.confirm(SHIELD, use=False, area_id=BEDROOM)
    assert str(failure.value) == "an ignored player is not placed"
    assert players.writes == []


async def test_confirm_is_unavailable_when_home_assistant_never_reports_the_change() -> None:
    players = FakePlayers(this_house(apple_tv=None))
    players.reports_writes = False
    service = ScreensService(players, confirm_timeout=0.05)
    async with asyncio.timeout(1.0):
        with pytest.raises(PlayersUnavailable) as failure:
            await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == "the house did not agree within 0.05s"
    assert players.writes == [("role", APPLE_TV, "use")]
    assert entity_of(players.snapshot(), APPLE_TV).role is None  # premise: never reported


async def test_a_refused_write_is_unavailable_in_the_port_s_words() -> None:
    service, players = _service(this_house(apple_tv=None))
    players.refuse = PortAuthFailed("Home Assistant refused label update: unauthorized")
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == "Home Assistant refused label update: unauthorized"
    assert players.writes == []


async def test_a_refused_area_write_leaves_the_role_in_place_and_a_retry_completes() -> None:
    refused = "Home Assistant refused config/device_registry/update: not_found"
    service, players = _service(this_house())
    assert entity_of(players.snapshot(), SHIELD).role is None  # premise: unconfirmed
    players.refuse_area = PortDataMalformed(refused)
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert failure.value.detail == refused
    assert players.writes == [("role", SHIELD, "use")]
    assert entity_of(players.snapshot(), SHIELD).role is DeviceRole.USE
    players.refuse_area = None
    view = await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert players.writes[1:] == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    screen = view.screen(BEDROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [SHIELD]


@pytest.mark.parametrize(
    ("players", "detail"),
    [(None, PLAYERS_OFF), (FakePlayers(), "the fake house has not been published")],
    ids=["off", "unread"],
)
async def test_confirm_with_no_house_is_unavailable_and_writes_nothing(
    players: FakePlayers | None, detail: str
) -> None:
    service = ScreensService(players, confirm_timeout=_TIMEOUT)
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == detail
    assert players is None or players.writes == []
