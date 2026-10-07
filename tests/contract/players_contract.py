"""Behaviour every `Players` implementation must satisfy, over `this_house()`."""

from abc import ABC, abstractmethod

import pytest

from tests.fakes.house import APPLE_TV, BEDROOM, SHIELD, SHIELD_DEVICE, this_house
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.players import Players


class PlayersHarness(ABC):
    """One implementation, already holding its first reading of `this_house()`."""

    players: Players

    @abstractmethod
    async def change_state(self, state: HouseState) -> None:
        """Change a player's reading at the registry, the way the household would."""

    @abstractmethod
    async def aclose(self) -> None: ...


def _role(house: HouseSnapshot, entity_id: str) -> DeviceRole | None:
    entity = house.entity(entity_id)
    assert entity is not None, f"{entity_id} is not in the snapshot"
    return entity.role


def _area(house: HouseSnapshot, entity_id: str) -> str | None:
    entity = house.entity(entity_id)
    assert entity is not None, f"{entity_id} is not in the snapshot"
    return house.area_of(entity)


def _reading(house: HouseSnapshot, entity_id: str) -> str | None:
    state = house.state(entity_id)
    return state.state if state is not None else None


class PlayersContract:
    """Subclass and provide an async `harness` fixture yielding a `PlayersHarness`."""

    async def test_the_snapshot_is_the_registry(self, harness: PlayersHarness) -> None:
        assert harness.players.snapshot() == this_house()

    async def test_a_role_is_written_and_reported_back(self, harness: PlayersHarness) -> None:
        assert _role(harness.players.snapshot(), SHIELD) is None, "the premise"
        await harness.players.set_role(SHIELD, DeviceRole.USE)
        house = await harness.players.wait_until(
            lambda house: _role(house, SHIELD) is DeviceRole.USE, timeout=1.0
        )
        assert _role(house, SHIELD) is DeviceRole.USE
        assert _role(house, APPLE_TV) is DeviceRole.USE, "no other player moved"

    async def test_a_new_role_replaces_the_old_one(self, harness: PlayersHarness) -> None:
        assert _role(harness.players.snapshot(), APPLE_TV) is DeviceRole.USE, "the premise"
        await harness.players.set_role(APPLE_TV, DeviceRole.IGNORE)
        house = await harness.players.wait_until(
            lambda house: _role(house, APPLE_TV) is not DeviceRole.USE, timeout=1.0
        )
        assert _role(house, APPLE_TV) is DeviceRole.IGNORE

    async def test_a_device_is_placed_and_reported_back(self, harness: PlayersHarness) -> None:
        assert _area(harness.players.snapshot(), SHIELD) is None, "the premise"
        await harness.players.set_device_area(SHIELD_DEVICE, BEDROOM)
        house = await harness.players.wait_until(
            lambda house: _area(house, SHIELD) is not None, timeout=1.0
        )
        assert _area(house, SHIELD) == BEDROOM

    async def test_a_change_at_the_registry_reaches_the_snapshot(
        self, harness: PlayersHarness
    ) -> None:
        assert _reading(harness.players.snapshot(), APPLE_TV) == "playing", "the premise"
        await harness.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        house = await harness.players.wait_until(
            lambda house: _reading(house, APPLE_TV) == "paused", timeout=1.0
        )
        assert _reading(house, APPLE_TV) == "paused"

    async def test_waiting_answers_at_once_when_the_house_already_agrees(
        self, harness: PlayersHarness
    ) -> None:
        house = await harness.players.wait_until(lambda house: True, timeout=0.01)
        assert house == harness.players.snapshot()

    async def test_waiting_for_what_never_comes_is_unavailable(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortUnavailable, match=r"within 0\.05s"):
            await harness.players.wait_until(lambda house: False, timeout=0.05)

    async def test_a_role_for_a_player_that_does_not_exist_is_refused(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await harness.players.set_role("media_player.nothing_here", DeviceRole.USE)

    async def test_placing_a_device_that_does_not_exist_is_refused(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await harness.players.set_device_area("d-nothing-here", BEDROOM)
