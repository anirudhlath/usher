"""The house's players, as the registry that owns them reports them."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from usher.domain.house import DeviceRole, HouseSnapshot


class Players(ABC):
    """A live reading of the house's registry, and the two writes Usher makes to it.

    The registry owns every area, device and player. An implementation writes
    only a player's role and a device's area, so the house never has two
    owners. With no connection a write raises as `snapshot` does; a write the
    registry refuses raises `PortAuthFailed` for the credential and
    `PortDataMalformed` for any other refusal.
    """

    @abstractmethod
    def snapshot(self) -> HouseSnapshot:
        """The latest reading, or why there is none.

        With none current, the error that ended the last connection, such as
        `PortAuthFailed` for a refused credential, or `PortUnavailable` before
        any connection has ended.
        """

    @abstractmethod
    async def set_role(self, entity_id: str, role: DeviceRole) -> None:
        """Mark a player to use or to ignore, keeping every other mark it carries."""

    @abstractmethod
    async def set_device_area(self, device_id: str, area_id: str) -> None:
        """Place a device in an area."""

    @abstractmethod
    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        """The current reading if `predicate` accepts it, else a later one.

        Readings published in one turn of the loop are seen only as the last of
        them. `PortUnavailable` naming `timeout` when none arrives in time. A write
        is done when the registry reports it, not when it was sent.
        """
