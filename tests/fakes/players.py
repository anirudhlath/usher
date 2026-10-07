"""`Players` over a snapshot a test publishes; a write lands only while `reports_writes`."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

from usher.domain.house import DeviceRole, HouseSnapshot
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.players import Players


class FakePlayers(Players):
    def __init__(self, house: HouseSnapshot | None = None) -> None:
        self._house = house
        self._changed = asyncio.Event()
        self.writes: list[tuple[str, str, str]] = []
        self.reports_writes = True
        self.refuse: UsherPortError | None = None

    def publish(self, house: HouseSnapshot) -> None:
        """Replace the reading and wake every waiter."""
        self._house = house
        self._changed.set()
        self._changed = asyncio.Event()

    def snapshot(self) -> HouseSnapshot:
        if self._house is None:
            raise PortUnavailable("the fake house has not been published")
        return self._house

    async def set_role(self, entity_id: str, role: DeviceRole) -> None:
        await asyncio.sleep(0)
        if self.refuse is not None:
            raise self.refuse
        house = self.snapshot()
        if house.entity(entity_id) is None:
            raise PortDataMalformed("the fake house has no such player")
        self.writes.append(("role", entity_id, role.value))
        if self.reports_writes:
            entities = tuple(
                replace(one, role=role) if one.entity_id == entity_id else one
                for one in house.entities
            )
            self.publish(house.with_registries(entities=entities))

    async def set_device_area(self, device_id: str, area_id: str) -> None:
        await asyncio.sleep(0)
        if self.refuse is not None:
            raise self.refuse
        house = self.snapshot()
        if all(one.device_id != device_id for one in house.devices):
            raise PortDataMalformed("the fake house has no such device")
        self.writes.append(("area", device_id, area_id))
        if self.reports_writes:
            devices = tuple(
                replace(one, area_id=area_id) if one.device_id == device_id else one
                for one in house.devices
            )
            self.publish(house.with_registries(devices=devices))

    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    changed = self._changed
                    if self._house is not None and predicate(self._house):
                        return self._house
                    await changed.wait()
        except TimeoutError:
            raise PortUnavailable(f"the house did not agree within {timeout:g}s") from None
