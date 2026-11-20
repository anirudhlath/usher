"""The house's players, as the registry that owns them reports them."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from usher.domain.house import DeviceRole, HouseSnapshot
from usher.ports.errors import PortDataMalformed


class CommandRefused(PortDataMalformed):
    """The registry refused a command for a reason asking again cannot change; `code` is its."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class Players(ABC):
    """A live reading of the house's registry, the two writes Usher makes to it, and two acts.

    The registry owns every area, device and player. An implementation writes
    only a player's role and a device's area, so the house never has two
    owners; beyond those it only wakes a player and hands it a URL. With no
    connection a write or an act raises as `snapshot` does; one the registry
    refuses raises `PortAuthFailed` for the credential and `PortDataMalformed`
    for any other refusal.
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
    async def wake(self, entity_id: str) -> None:
        """Ask a player to wake; done when the registry has taken the request.

        That is not when the player is awake.
        """

    @abstractmethod
    async def play_url(self, entity_id: str, url: str) -> None:
        """Hand a player a URL to open.

        `PortDataMalformed` when the registry refuses, which is what a sleeping
        player looks like, and `CommandRefused` when no waking can change the
        answer; `PortUnavailable` with no connection or no answer in time;
        `PortAuthFailed` for the credential. The URL is a credential and appears
        in no error.
        """

    @abstractmethod
    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        """The current reading if `predicate` accepts it, else a later one.

        Readings published in one turn of the loop are seen only as the last of
        them. `PortUnavailable` naming `timeout` when none arrives in time, and
        what `snapshot` would raise once a reading the wait has seen is gone. A
        write is done when the registry reports it, not when it was sent.
        """
