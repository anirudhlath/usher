"""Screens: the house as a view, and confirming a player in the registry that owns it."""

from typing import Final

from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import HouseView, ScreenDevice, project
from usher.ports.errors import UsherPortError
from usher.ports.players import Players

PLAYERS_OFF: Final = "players are off: USHER_PLAYERS_ENABLED is false"


class PlayersUnavailable(Exception):
    """The house cannot be read or written now. `detail` is safe to show."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownCandidate(LookupError):
    """No player Usher could play on has this entity id."""


class UnknownArea(LookupError):
    """The registry has no area with this id."""


class CannotPlace(ValueError):
    """Only a player to use, with no area, on a device, with no area of its own, is placed.

    Its message names no submitted value, so a route may show it as it is.
    """


class ScreensService:
    def __init__(self, players: Players | None, *, confirm_timeout: float) -> None:
        self._players = players
        self._confirm_timeout = confirm_timeout

    def house(self) -> HouseView:
        """The latest reading, projected onto screens."""
        return project(self._reading()[1])

    async def confirm(self, entity_id: str, *, use: bool, area_id: str | None = None) -> HouseView:
        """Mark a candidate to use or to ignore, placing one to use when asked.

        Every check runs before the first write, so a confirm a check refuses
        writes nothing. An ignored player is never placed, since Usher writes
        nothing more to a player the owner told it to leave alone. The answer
        is the house once the registry reports every write; a write it
        refuses, or never reports within the timeout, is `PlayersUnavailable`
        in the port's words, which never carry the registry's own message.
        A write refused after the first leaves the first in place, and
        confirming again is safe, since every check still passes.
        """
        players, house = self._reading()
        player = project(house).candidate(entity_id)
        if player is None:
            raise UnknownCandidate(entity_id)
        if area_id is not None and not use:
            raise CannotPlace("an ignored player is not placed")
        placement = None if area_id is None else (_device_to_place(house, player, area_id), area_id)
        role = DeviceRole.USE if use else DeviceRole.IGNORE
        try:
            await players.set_role(entity_id, role)
            if placement is not None:
                await players.set_device_area(*placement)
            landed = await players.wait_until(
                lambda now: _landed(now, entity_id, role, area_id),
                timeout=self._confirm_timeout,
            )
        except UsherPortError as error:
            raise PlayersUnavailable(str(error)) from error
        return project(landed)

    def _reading(self) -> tuple[Players, HouseSnapshot]:
        if self._players is None:
            raise PlayersUnavailable(PLAYERS_OFF)
        try:
            return self._players, self._players.snapshot()
        except UsherPortError as error:
            raise PlayersUnavailable(str(error)) from error


def _device_to_place(house: HouseSnapshot, player: ScreenDevice, area_id: str) -> str:
    """The device whose area to set, or why there is none.

    An entity's own area overrides its device's, so a player whose entity
    names an area, even a deleted one, would never move with its device.
    """
    if house.area(area_id) is None:
        raise UnknownArea(area_id)
    if player.area_id is not None:
        raise CannotPlace("the player is already in an area; move it in the registry")
    if player.device_id is None:
        raise CannotPlace("the player belongs to no device, so it has no area to set")
    entity = house.entity(player.entity_id)
    if entity is not None and entity.area_id is not None:
        raise CannotPlace("the player carries an area of its own; change it in the registry")
    return player.device_id


def _landed(house: HouseSnapshot, entity_id: str, role: DeviceRole, area_id: str | None) -> bool:
    entity = house.entity(entity_id)
    if entity is None or entity.role is not role:
        return False
    return area_id is None or house.area_of(entity) == area_id
