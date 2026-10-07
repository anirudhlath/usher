"""Screens: the house projected onto what Usher can play on, recomputed on every read."""

from dataclasses import dataclass
from typing import Final

from usher.domain.house import DeviceRole, HouseArea, HouseEntity, HouseSnapshot, HouseState
from usher.domain.recipes import RECIPES, Recipe, recipes_for

PLAYER_DOMAIN: Final = "media_player."


@dataclass(frozen=True, slots=True)
class ScreenDevice:
    """One player Usher could play on, with HA's live reading of it."""

    entity_id: str
    name: str
    integration: str
    model: str | None
    area_id: str | None
    device_id: str | None
    role: DeviceRole | None
    recipes: tuple[Recipe, ...]
    state: HouseState | None


@dataclass(frozen=True, slots=True)
class Screen:
    """An HA area holding at least one confirmed player."""

    area_id: str
    name: str
    devices: tuple[ScreenDevice, ...]


@dataclass(frozen=True, slots=True)
class HouseView:
    """Every candidate in exactly one of four lists, and every area to place one in."""

    screens: tuple[Screen, ...]
    unconfirmed: tuple[ScreenDevice, ...]
    no_area: tuple[ScreenDevice, ...]
    ignored: tuple[ScreenDevice, ...]
    areas: tuple[HouseArea, ...]

    def screen(self, area_id: str) -> Screen | None:
        return next((screen for screen in self.screens if screen.area_id == area_id), None)

    def candidate(self, entity_id: str) -> ScreenDevice | None:
        on_screens = (player for screen in self.screens for player in screen.devices)
        everyone = (*on_screens, *self.unconfirmed, *self.no_area, *self.ignored)
        return next((player for player in everyone if player.entity_id == entity_id), None)


def _candidate(
    house: HouseSnapshot, entity: HouseEntity, recipes: tuple[Recipe, ...]
) -> ScreenDevice | None:
    if not entity.entity_id.startswith(PLAYER_DOMAIN) or entity.disabled:
        return None
    model = house.model_of(entity)
    matched = recipes_for(entity.integration, model, recipes)
    if not matched:
        return None
    area_id = house.area_of(entity)
    return ScreenDevice(
        entity_id=entity.entity_id,
        name=house.display_name(entity),
        integration=entity.integration,
        model=model,
        area_id=area_id if area_id is not None and house.area(area_id) is not None else None,
        device_id=entity.device_id,
        role=entity.role,
        recipes=matched,
        state=house.state(entity.entity_id),
    )


def _by_name(players: list[ScreenDevice]) -> tuple[ScreenDevice, ...]:
    return tuple(sorted(players, key=lambda player: (player.name.casefold(), player.entity_id)))


def project(house: HouseSnapshot, recipes: tuple[Recipe, ...] = RECIPES) -> HouseView:
    """Snapshot to screens.

    A candidate is an enabled `media_player` entity on a device some recipe
    answers to by integration *and* model, so Music Assistant's copy of a TV, a
    Cast speaker, a TV no recipe knows and a group player with no device are
    never candidates. Its area is HA's: the entity's own, else its device's. An
    area id naming no area is no area, so a deleted area never becomes a screen
    named by its id. Every list is in reading order: names compared without
    case, ids breaking ties.
    """
    grouped: dict[str, list[ScreenDevice]] = {}
    unconfirmed: list[ScreenDevice] = []
    no_area: list[ScreenDevice] = []
    ignored: list[ScreenDevice] = []
    for entity in house.entities:
        player = _candidate(house, entity, recipes)
        if player is None:
            continue
        if player.role is None:
            unconfirmed.append(player)
        elif player.role is DeviceRole.IGNORE:
            ignored.append(player)
        elif player.area_id is None:
            no_area.append(player)
        else:
            grouped.setdefault(player.area_id, []).append(player)
    screens = tuple(
        sorted(
            (
                Screen(area_id=area_id, name=_area_name(house, area_id), devices=_by_name(players))
                for area_id, players in grouped.items()
            ),
            key=lambda screen: (screen.name.casefold(), screen.area_id),
        )
    )
    return HouseView(
        screens=screens,
        unconfirmed=_by_name(unconfirmed),
        no_area=_by_name(no_area),
        ignored=_by_name(ignored),
        areas=tuple(sorted(house.areas, key=lambda one: (one.name.casefold(), one.area_id))),
    )


def _area_name(house: HouseSnapshot, area_id: str) -> str:
    found = house.area(area_id)
    return found.name if found is not None else area_id
