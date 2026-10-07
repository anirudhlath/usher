"""The `Players` contract on the fake, and what only the fake can be asked to do."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
import pytest_asyncio

from tests.contract.players_contract import PlayersContract, PlayersHarness
from tests.fakes.house import APPLE_TV, BEDROOM, SHIELD, SHIELD_DEVICE, this_house
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortAuthFailed, PortUnavailable

type _Write = Callable[[FakePlayers], Coroutine[Any, Any, None]]

# Both writes, so an affordance honoured by one and not the other cannot pass.
_WRITES: dict[str, _Write] = {
    "role": lambda players: players.set_role(SHIELD, DeviceRole.USE),
    "area": lambda players: players.set_device_area(SHIELD_DEVICE, BEDROOM),
}
each_write = pytest.mark.parametrize("write", list(_WRITES.values()), ids=list(_WRITES))


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


@each_write
async def test_a_fake_that_never_reports_a_write_leaves_the_house_unchanged(
    write: _Write,
) -> None:
    players = FakePlayers(this_house())
    players.reports_writes = False
    await write(players)
    assert len(players.writes) == 1, "the premise: the write was sent"
    assert players.snapshot() == this_house()
    with pytest.raises(PortUnavailable):
        await players.wait_until(lambda house: house != this_house(), timeout=0.01)


@each_write
async def test_a_refusing_fake_raises_what_it_was_given_and_writes_nothing(
    write: _Write,
) -> None:
    players = FakePlayers(this_house())
    players.refuse = refusal = PortAuthFailed("refused")
    with pytest.raises(PortAuthFailed) as raised:
        await write(players)
    assert raised.value is refusal
    assert players.writes == []
    assert players.snapshot() == this_house()


@each_write
def test_each_write_yields_to_the_loop_before_it_lands(write: _Write) -> None:
    """Driven by hand: a write that never awaits gives a caller's `wait_for` nothing to cancel."""
    players = FakePlayers(this_house())
    writing = write(players)
    try:
        writing.send(None)
    except StopIteration:
        pytest.fail("the write landed without yielding to the loop")
    assert players.writes == [], "nothing lands before the yield"
    with pytest.raises(StopIteration):
        writing.send(None)
    assert len(players.writes) == 1, "the premise: resumed, the write lands"


async def test_a_waiter_on_an_unpublished_house_waits_for_the_first_reading() -> None:
    players = FakePlayers()
    waiting = asyncio.create_task(players.wait_until(lambda house: True, timeout=1.0))
    await asyncio.sleep(0)
    assert not waiting.done(), "nothing has been published to agree with"
    players.publish(this_house())
    assert await waiting == this_house()


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
