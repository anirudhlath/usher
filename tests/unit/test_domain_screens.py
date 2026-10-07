"""The projection, over this house and the shapes it can take."""

from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    APPLE_TV_DEVICE,
    BEDROOM,
    KITCHEN,
    LG,
    LIVING_ROOM,
    MA_DUPLICATE,
    NEST,
    PS5,
    SHIELD,
    SHIELD_DEVICE,
    area,
    device,
    entity,
    entity_of,
    snapshot,
    this_house,
)
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.recipes import RECIPES, Recipe, recipes_for
from usher.domain.screens import HouseView, ScreenDevice, project

# Invented: the remote HA's `apple_tv` integration adds beside each media player.
APPLE_TV_REMOTE = "remote.living_room_apple_tv"
_NOTHING_PLACED: dict[str, object] = {
    "screens": {},
    "unconfirmed": [],
    "no_area": [],
    "ignored": [],
}
_BY_NAME = ["media_player.z", "media_player.x", "media_player.y"]


def _labelled(house: HouseSnapshot, entity_id: str, role: DeviceRole | None) -> HouseSnapshot:
    return house.with_registries(
        entities=tuple(
            replace(one, role=role) if one.entity_id == entity_id else one for one in house.entities
        )
    )


def _moved(house: HouseSnapshot, device_id: str, area_id: str | None) -> HouseSnapshot:
    return house.with_registries(
        devices=tuple(
            replace(one, area_id=area_id) if one.device_id == device_id else one
            for one in house.devices
        )
    )


def _everyone(view: HouseView) -> list[ScreenDevice]:
    on_screens = [player for screen in view.screens for player in screen.devices]
    return [*on_screens, *view.unconfirmed, *view.no_area, *view.ignored]


def _placed(view: HouseView) -> dict[str, object]:
    """Every list's players by id, each screen's under its area id."""
    return {
        "screens": {
            screen.area_id: [player.entity_id for player in screen.devices]
            for screen in view.screens
        },
        "unconfirmed": [player.entity_id for player in view.unconfirmed],
        "no_area": [player.entity_id for player in view.no_area],
        "ignored": [player.entity_id for player in view.ignored],
    }


def _answering(house: HouseSnapshot, entity_id: str) -> tuple[Recipe, ...]:
    """One recipe answering to the entity's own integration and model."""
    one = entity_of(house, entity_id)
    model = house.model_of(one)
    assert model is not None, f"{entity_id} has no model"
    return (
        replace(
            RECIPES[0], key="test.answering", integration=one.integration, model_prefixes=(model,)
        ),
    )


def _three_players(role: DeviceRole | None, area_id: str | None) -> HouseSnapshot:
    """Three SHIELDs whose id order, arrival order and name order are three different orders."""
    names = {"z": "Attic", "x": "Bedroom", "y": "Cellar"}
    return snapshot(
        areas=(area("den"),),
        devices=(device("dev-den", model="SHIELD Android TV", area_id=area_id),),
        entities=tuple(
            entity(
                f"media_player.{key}",
                integration="cast",
                device_id="dev-den",
                name=f"Player {names[key]}",
                role=role,
            )
            for key in ("y", "z", "x")
        ),
    )


def test_this_house_has_one_screen_holding_the_apple_tv() -> None:
    view = project(this_house())
    assert [(screen.area_id, screen.name) for screen in view.screens] == [
        (LIVING_ROOM, "Living Room")
    ]
    (player,) = view.screens[0].devices
    assert player.entity_id == APPLE_TV
    assert player.name == "Living Room Apple TV"
    assert tuple(recipe.key for recipe in player.recipes) == ("apple_tv.infuse", "apple_tv.airplay")


def test_a_candidate_carries_its_registry_facts() -> None:
    house = this_house()
    stored = entity_of(house, APPLE_TV)
    apple_tv_device = house.device(APPLE_TV_DEVICE)
    assert apple_tv_device is not None
    player = project(house).candidate(APPLE_TV)
    assert player is not None
    assert (player.integration, player.model, player.area_id, player.device_id, player.role) == (
        stored.integration,
        apple_tv_device.model,
        LIVING_ROOM,
        APPLE_TV_DEVICE,
        DeviceRole.USE,
    )


def test_music_assistant_s_copy_is_never_a_candidate_even_when_labelled() -> None:
    house = _labelled(this_house(), MA_DUPLICATE, DeviceRole.USE)
    assert house.entity(MA_DUPLICATE) is not None, "the premise: the entity is in the house"
    assert project(house, _answering(house, MA_DUPLICATE)).candidate(MA_DUPLICATE) is not None, (
        "the premise: only the recipes keep it out"
    )
    assert project(house).candidate(MA_DUPLICATE) is None


@pytest.mark.parametrize("entity_id", [LG, PS5, NEST])
def test_a_player_no_recipe_answers_to_is_never_a_candidate(entity_id: str) -> None:
    house = _labelled(this_house(), entity_id, DeviceRole.USE)
    assert house.entity(entity_id) is not None, "the premise: the entity is in the house"
    assert project(house, _answering(house, entity_id)).candidate(entity_id) is not None, (
        "the premise: only the recipes keep it out"
    )
    assert project(house).candidate(entity_id) is None


def test_only_the_recipes_given_are_consulted() -> None:
    (airplay,) = (recipe for recipe in RECIPES if recipe.key == "apple_tv.airplay")
    view = project(this_house(), recipes=(airplay,))
    assert [(player.entity_id, player.recipes) for player in _everyone(view)] == [
        (APPLE_TV, (airplay,))
    ]


def test_only_a_media_player_is_a_candidate() -> None:
    house = this_house()
    remote = entity(
        APPLE_TV_REMOTE, integration="apple_tv", device_id=APPLE_TV_DEVICE, role=DeviceRole.USE
    )
    with_remote = house.with_registries(entities=(*house.entities, remote))
    assert recipes_for(remote.integration, with_remote.model_of(remote)), (
        "the premise: a recipe answers to the remote's integration and model"
    )
    assert project(with_remote).candidate(APPLE_TV_REMOTE) is None


def test_the_shield_waits_unconfirmed_with_no_area() -> None:
    view = project(this_house())
    assert [player.entity_id for player in view.unconfirmed] == [SHIELD]
    assert view.unconfirmed[0].area_id is None
    assert view.unconfirmed[0].device_id == SHIELD_DEVICE


def test_a_confirmed_player_with_no_area_is_listed_under_no_area_and_on_no_screen() -> None:
    view = project(this_house(shield=DeviceRole.USE))
    assert [player.entity_id for player in view.no_area] == [SHIELD]
    assert [screen.area_id for screen in view.screens] == [LIVING_ROOM]


def test_an_ignored_player_is_listed_as_ignored_and_on_no_screen() -> None:
    view = project(this_house(apple_tv=DeviceRole.IGNORE))
    assert [player.entity_id for player in view.ignored] == [APPLE_TV]
    assert view.screens == ()


@pytest.mark.parametrize(
    ("role", "placed"),
    [
        pytest.param(None, {"unconfirmed": [APPLE_TV, SHIELD]}, id="unconfirmed"),
        pytest.param(
            DeviceRole.USE,
            {"screens": {LIVING_ROOM: [APPLE_TV]}, "no_area": [SHIELD]},
            id="use",
        ),
        pytest.param(DeviceRole.IGNORE, {"ignored": [APPLE_TV, SHIELD]}, id="ignore"),
    ],
)
def test_a_player_s_role_then_its_area_decide_its_one_list(
    role: DeviceRole | None, placed: dict[str, object]
) -> None:
    house = this_house(apple_tv=role, shield=role)
    areas = (house.area_of(entity_of(house, APPLE_TV)), house.area_of(entity_of(house, SHIELD)))
    assert areas == (LIVING_ROOM, None), "the premise: the Apple TV has an area and the SHIELD none"
    view = project(house)
    assert _placed(view) == {**_NOTHING_PLACED, **placed}
    everyone = _everyone(view)
    assert [view.candidate(player.entity_id) for player in everyone] == everyone


def test_a_disabled_entity_is_never_a_candidate() -> None:
    house = this_house()
    disabled = house.with_registries(
        entities=tuple(
            replace(one, disabled=True) if one.entity_id == APPLE_TV else one
            for one in house.entities
        )
    )
    assert project(house).candidate(APPLE_TV) is not None, "the premise"
    assert project(disabled).candidate(APPLE_TV) is None


def test_a_player_moved_between_areas_moves_screen() -> None:
    view = project(_moved(this_house(), APPLE_TV_DEVICE, BEDROOM))
    assert [screen.area_id for screen in view.screens] == [BEDROOM]


def test_an_entity_s_own_area_outranks_its_device_s() -> None:
    house = this_house()
    apple_tv_device = house.device(APPLE_TV_DEVICE)
    assert apple_tv_device is not None and apple_tv_device.area_id == LIVING_ROOM, (
        "the premise: the Apple TV's device is in the Living Room"
    )
    overridden = house.with_registries(
        entities=tuple(
            replace(one, area_id=BEDROOM) if one.entity_id == APPLE_TV else one
            for one in house.entities
        )
    )
    assert [screen.area_id for screen in project(overridden).screens] == [BEDROOM]


def test_an_area_id_naming_no_area_is_no_area() -> None:
    view = project(_moved(this_house(), APPLE_TV_DEVICE, "deleted_area"))
    assert view.screens == ()
    assert [player.entity_id for player in view.no_area] == [APPLE_TV]


def test_a_screen_is_found_by_its_area_and_an_area_with_no_device_is_no_screen() -> None:
    house = this_house()
    assert house.area(KITCHEN) is not None, "the premise: the Kitchen is an area"
    view = project(house)
    assert view.screen(LIVING_ROOM) is view.screens[0]
    assert view.screen(KITCHEN) is None


def test_screens_and_areas_are_ordered_by_name_not_by_id_or_arrival() -> None:
    """Ids chosen so id order, arrival order and name order are three different orders."""
    names = {"z": "Attic", "x": "Bedroom", "y": "Cellar"}
    house = snapshot(
        areas=tuple(area(area_id, names[area_id]) for area_id in ("y", "z", "x")),
        devices=tuple(
            device(f"dev-{area_id}", model="SHIELD Android TV", area_id=area_id)
            for area_id in ("y", "z", "x")
        ),
        entities=tuple(
            entity(
                f"media_player.{area_id}",
                integration="cast",
                device_id=f"dev-{area_id}",
                name=f"Player {names[area_id]}",
                role=DeviceRole.USE,
            )
            for area_id in ("y", "z", "x")
        ),
    )
    in_id_order = [one.name for one in house.areas]
    assert in_id_order != sorted(in_id_order), "the premise: id order is not name order"
    view = project(house)
    assert [screen.name for screen in view.screens] == ["Attic", "Bedroom", "Cellar"]
    assert [one.name for one in view.areas] == ["Attic", "Bedroom", "Cellar"]


@pytest.mark.parametrize(
    ("role", "area_id", "placed"),
    [
        pytest.param(DeviceRole.USE, "den", {"screens": {"den": _BY_NAME}}, id="screen"),
        pytest.param(DeviceRole.USE, None, {"no_area": _BY_NAME}, id="no_area"),
        pytest.param(None, "den", {"unconfirmed": _BY_NAME}, id="unconfirmed"),
        pytest.param(DeviceRole.IGNORE, "den", {"ignored": _BY_NAME}, id="ignored"),
    ],
)
def test_players_are_ordered_by_name_in_every_list(
    role: DeviceRole | None, area_id: str | None, placed: dict[str, object]
) -> None:
    house = _three_players(role, area_id)
    in_id_order = [one.entity_id for one in house.entities]
    in_name_order = [one.entity_id for one in sorted(house.entities, key=lambda e: e.name or "")]
    assert in_id_order != in_name_order, "the premise: id order is not name order"
    assert _placed(project(house)) == {**_NOTHING_PLACED, **placed}


def test_each_player_carries_ha_s_reading_unchanged() -> None:
    house = this_house()
    assert house.state(APPLE_TV) is not None, "the premise: HA reports the Apple TV's state"
    player = project(house).candidate(APPLE_TV)
    assert player is not None
    assert player.state is house.state(APPLE_TV)


def test_every_area_is_offered_for_placing_a_player() -> None:
    assert [one.name for one in project(this_house()).areas] == [
        "Bedroom",
        "Kitchen",
        "Living Room",
    ]


def test_every_candidate_is_in_exactly_one_list() -> None:
    view = project(this_house(shield=DeviceRole.USE))
    ids = [player.entity_id for player in _everyone(view)]
    assert sorted(ids) == sorted({APPLE_TV, SHIELD})
