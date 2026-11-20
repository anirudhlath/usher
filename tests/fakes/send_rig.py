"""The parts every test that drives a `SendDispatcher` shares: a house, parking players, a clock."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.fakes.house import area, device, entity, snapshot, state
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.ports.events import ClientEvent, EventPublisher

PLAYER = "media_player.living_room_living_room_apple_tv"
BEDROOM_PLAYER = "media_player.bedroom_bedroom_apple_tv"
# Long enough for any arrangement here, short enough that a hang fails fast.
DEADLINE_SECONDS = 2.0


def two_screen_house() -> HouseSnapshot:
    """A Living Room and a Bedroom, each with a confirmed Apple TV on standby."""

    def player(entity_id: str, device_id: str, name: str, area_id: str) -> tuple[Any, ...]:
        return (
            device(
                device_id,
                name=name,
                model="Apple TV 4K (gen 3)",
                manufacturer="Apple",
                area_id=area_id,
            ),
            entity(entity_id, integration="apple_tv", device_id=device_id, role=DeviceRole.USE),
            state(entity_id, "standby", friendly_name=name),
        )

    living = player(
        PLAYER, "d1000000000000000000000000000001", "Living Room Apple TV", "living_room"
    )
    bedroom = player(
        BEDROOM_PLAYER, "d1000000000000000000000000000002", "Bedroom Apple TV", "bedroom"
    )
    return snapshot(
        areas=(area("living_room", "Living Room"), area("bedroom", "Bedroom")),
        devices=(living[0], bedroom[0]),
        entities=(living[1], bedroom[1]),
        states=(living[2], bedroom[2]),
    )


class ParkingPlayers(FakePlayers):
    """`FakePlayers` that counts the waits a send is parked in, with a hook before each.

    `on_wait` gets the wait's ordinal, so a case changes the house as a stage
    begins rather than racing that stage's window.
    """

    def __init__(self, house: HouseSnapshot) -> None:
        super().__init__(house)
        self.parked = 0
        self.waits = 0
        self.on_wait: Callable[[int], None] | None = None
        self.on_wake: Callable[[], None] | None = None

    async def wake(self, entity_id: str) -> None:
        await super().wake(entity_id)
        if self.on_wake is not None:
            self.on_wake()

    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        self.waits += 1
        if self.on_wait is not None:
            self.on_wait(self.waits)
        self.parked += 1
        try:
            return await super().wait_until(predicate, timeout=timeout)
        finally:
            self.parked -= 1


class Recording(EventPublisher):
    def __init__(self) -> None:
        self.events: list[ClientEvent] = []

    async def publish(self, event: ClientEvent) -> None:
        self.events.append(event)


@dataclass
class FakeTime:
    """A monotonic clock that only the dispatcher's own `sleep` moves."""

    now: float = 1_000.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def settle_parked(players: ParkingPlayers, parked: int = 1) -> None:
    """Until `parked` sends wait on the house; a send that never parks fails the case."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        await asyncio.sleep(0.001)
        if players.parked >= parked:
            return
        assert time.monotonic() < deadline, f"{players.parked} of {parked} sends parked"
