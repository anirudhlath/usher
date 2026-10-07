"""The `Players` contract on the fake, and what only the fake can be asked to do."""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from tests.contract.players_contract import PlayersContract, PlayersHarness
from tests.fakes.house import APPLE_TV, BEDROOM, SHIELD, SHIELD_DEVICE, this_house
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortAuthFailed, PortUnavailable


class _FakeHarness(PlayersHarness):
    def __init__(self) -> None:
        self.players = self.fake = FakePlayers(this_house())

    async def change_state(self, state: HouseState) -> None:
        self.fake.publish(self.fake.snapshot().with_state(state))

    async def aclose(self) -> None:
        pass


class TestFakePlayers(PlayersContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[PlayersHarness]:
        yield _FakeHarness()


def test_an_unpublished_house_is_unavailable() -> None:
    with pytest.raises(PortUnavailable):
        FakePlayers().snapshot()


async def test_writes_are_recorded_in_order() -> None:
    players = FakePlayers(this_house())
    await players.set_role(SHIELD, DeviceRole.USE)
    await players.set_device_area(SHIELD_DEVICE, BEDROOM)
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]


async def test_a_fake_that_never_reports_a_write_leaves_the_house_unchanged() -> None:
    players = FakePlayers(this_house())
    players.reports_writes = False
    await players.set_role(SHIELD, DeviceRole.USE)
    assert players.snapshot() == this_house()
    with pytest.raises(PortUnavailable):
        await players.wait_until(lambda house: house != this_house(), timeout=0.01)


async def test_a_refusing_fake_raises_what_it_was_given_and_writes_nothing() -> None:
    players = FakePlayers(this_house())
    players.refuse = PortAuthFailed("refused")
    with pytest.raises(PortAuthFailed):
        await players.set_role(SHIELD, DeviceRole.USE)
    assert players.writes == []


async def test_a_waiter_sleeps_through_an_unrelated_publish_and_wakes_on_the_next() -> None:
    players = FakePlayers(this_house())
    paused = HouseState(entity_id=APPLE_TV, state="paused")

    def is_paused(house: HouseSnapshot) -> bool:
        return house.state(APPLE_TV) == paused

    waiting = asyncio.create_task(players.wait_until(is_paused, timeout=1.0))
    await asyncio.sleep(0)
    players.publish(this_house(shield=DeviceRole.IGNORE))
    await asyncio.sleep(0)
    assert not waiting.done(), "the premise: the unrelated publish satisfied nothing"
    players.publish(this_house().with_state(paused))
    assert (await waiting).state(APPLE_TV) == paused
