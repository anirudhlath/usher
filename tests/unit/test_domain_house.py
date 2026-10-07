"""`HouseSnapshot`: what Home Assistant reported, and HA's own precedence over it."""

from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    SHIELD,
    area,
    device,
    entity,
    entity_of,
    snapshot,
    state,
    this_house,
)
from usher.domain.house import DeviceRole

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


def test_an_entity_with_no_area_and_no_device_has_none() -> None:
    house = snapshot(entities=(entity(PLAYER, integration="cast", device_id="missing"),))
    assert house.area_of(entity_of(house, PLAYER)) is None


@pytest.mark.parametrize(
    ("with_state", "entity_name", "with_device", "expected"),
    [
        (True, "Registry name", True, "Friendly"),
        (False, "Registry name", True, "Registry name"),
        (False, None, True, "Device name"),
        (False, None, False, PLAYER),
    ],
)
def test_display_name_prefers_ha_s_friendly_name_then_registry_then_device_then_id(
    with_state: bool, entity_name: str | None, with_device: bool, expected: str
) -> None:
    house = snapshot(
        devices=(device("d", name="Device name"),) if with_device else (),
        entities=(entity(PLAYER, integration="cast", device_id="d", name=entity_name),),
        states=(state(PLAYER, friendly_name="Friendly"),) if with_state else (),
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
    renamed = house.with_registries(areas=(area("living_room", "Lounge"),))
    assert renamed.areas == (area("living_room", "Lounge"),)
    assert (renamed.devices, renamed.entities, renamed.states) == (
        house.devices,
        house.entities,
        house.states,
    )


def test_equality_ignores_the_order_rows_arrived_in() -> None:
    """A 3-cycle, not a swap: a reversal is its own inverse and can hide a sort by accident."""
    one, two, three = state("media_player.a"), state("media_player.b"), state("media_player.c")
    assert snapshot(states=(one, two, three)) == snapshot(states=(three, one, two))
    assert snapshot(states=(three, one, two)).states == (one, two, three)


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
