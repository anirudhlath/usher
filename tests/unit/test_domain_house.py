"""`HouseSnapshot`: what Home Assistant reported, and HA's own precedence over it."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    LIVING_ROOM,
    SHIELD,
    SHIELD_DEVICE,
    area,
    device,
    entity,
    entity_of,
    snapshot,
    state,
    this_house,
)
from usher.domain.house import DeviceRole, HouseSnapshot

PLAYER = "media_player.x"


def test_an_entity_with_its_own_area_overrides_its_device() -> None:
    house = snapshot(
        areas=(area("a"), area("b")),
        devices=(device("d", area_id="a"),),
        entities=(entity(PLAYER, integration="cast", device_id="d", area_id="b"),),
    )
    assert house.area_of(entity_of(house, PLAYER)) == "b"


def test_an_entity_with_no_area_takes_its_device_area() -> None:
    house = snapshot(
        areas=(area("a"),),
        devices=(device("d", area_id="a"),),
        entities=(entity(PLAYER, integration="cast", device_id="d"),),
    )
    assert house.area_of(entity_of(house, PLAYER)) == "a"


@pytest.mark.parametrize("device_id", [None, "missing"])
def test_an_entity_with_no_area_and_no_device_has_none(device_id: str | None) -> None:
    """Another device has an area, so `None` is an answer about this entity's device."""
    house = snapshot(
        areas=(area("a"),),
        devices=(device("d", area_id="a"),),
        entities=(entity(PLAYER, integration="cast", device_id=device_id),),
    )
    assert house.area_of(entity_of(house, PLAYER)) is None


@pytest.mark.parametrize(
    ("with_state", "friendly_name", "entity_name", "with_device", "expected"),
    [
        pytest.param(True, "Friendly", "Registry name", True, "Friendly", id="friendly"),
        pytest.param(True, None, "Registry name", True, "Registry name", id="friendly-none"),
        pytest.param(True, "", "Registry name", True, "Registry name", id="friendly-empty"),
        pytest.param(False, None, "Registry name", True, "Registry name", id="no-state"),
        pytest.param(False, None, "", True, "Device name", id="registry-empty"),
        pytest.param(False, None, None, True, "Device name", id="registry-none"),
        pytest.param(False, None, None, False, PLAYER, id="no-device"),
    ],
)
def test_display_name_prefers_ha_s_friendly_name_then_registry_then_device_then_id(
    with_state: bool,
    friendly_name: str | None,
    entity_name: str | None,
    with_device: bool,
    expected: str,
) -> None:
    house = snapshot(
        devices=(device("d", name="Device name"),) if with_device else (),
        entities=(entity(PLAYER, integration="cast", device_id="d", name=entity_name),),
        states=(state(PLAYER, friendly_name=friendly_name),) if with_state else (),
    )
    assert house.display_name(entity_of(house, PLAYER)) == expected


def test_with_state_replaces_rather_than_adds() -> None:
    house = this_house().with_state(state(SHIELD, "playing"))
    assert [one.state for one in house.states if one.entity_id == SHIELD] == ["playing"]
    assert len(house.states) == len(this_house().states)


def test_without_state_removes_only_that_entity() -> None:
    house = this_house().without_state(SHIELD)
    assert house.state(SHIELD) is None
    assert house.state(APPLE_TV) == this_house().state(APPLE_TV)
    assert len(house.states) == len(this_house().states) - 1


def test_with_registries_keeps_what_it_was_not_given() -> None:
    house = this_house()
    lounge = (area(LIVING_ROOM, "Lounge"),)
    renamed = house.with_registries(areas=lounge)
    assert renamed.areas == lounge
    assert (renamed.devices, renamed.entities, renamed.states) == (
        house.devices,
        house.entities,
        house.states,
    )
    placed = (device(SHIELD_DEVICE, name="SHIELD", area_id=LIVING_ROOM),)
    moved = house.with_registries(devices=placed)
    assert moved.devices == placed
    assert (moved.areas, moved.entities, moved.states) == (
        house.areas,
        house.entities,
        house.states,
    )


@pytest.mark.parametrize(
    ("build", "ids_of"),
    [
        pytest.param(
            lambda ids: snapshot(areas=tuple(area(one) for one in ids)),
            lambda house: [one.area_id for one in house.areas],
            id="areas",
        ),
        pytest.param(
            lambda ids: snapshot(devices=tuple(device(one) for one in ids)),
            lambda house: [one.device_id for one in house.devices],
            id="devices",
        ),
        pytest.param(
            lambda ids: snapshot(entities=tuple(entity(one, integration="cast") for one in ids)),
            lambda house: [one.entity_id for one in house.entities],
            id="entities",
        ),
        pytest.param(
            lambda ids: snapshot(states=tuple(state(one) for one in ids)),
            lambda house: [one.entity_id for one in house.states],
            id="states",
        ),
    ],
)
def test_equality_ignores_the_order_rows_arrived_in(
    build: Callable[[tuple[str, ...]], HouseSnapshot],
    ids_of: Callable[[HouseSnapshot], list[str]],
) -> None:
    """A 3-cycle, not a swap: a reversal is its own inverse and can hide a sort by accident."""
    in_order, cycled = ("a", "b", "c"), ("c", "a", "b")
    assert ids_of(build(in_order)) == ["a", "b", "c"], "the premise: the case reads its own rows"
    assert build(cycled) == build(in_order)
    assert ids_of(build(cycled)) == ["a", "b", "c"]


def test_a_new_snapshot_answers_from_its_own_rows_not_an_old_index() -> None:
    house = this_house()
    assert entity_of(house, SHIELD).role is None, "the premise, and it builds the old index"
    confirmed = house.with_registries(
        entities=tuple(
            replace(one, role=DeviceRole.USE) if one.entity_id == SHIELD else one
            for one in house.entities
        )
    )
    assert entity_of(confirmed, SHIELD).role is DeviceRole.USE
    assert entity_of(house, SHIELD).role is None
