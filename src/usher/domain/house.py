"""The house as Home Assistant reports it: areas, devices, players and what they show.

Usher keeps none of it: a snapshot lives in memory and is replaced on every event.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import cached_property


class DeviceRole(StrEnum):
    """What the owner decided about a player, as a label in Home Assistant carries it."""

    USE = "use"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class HouseArea:
    area_id: str
    name: str


@dataclass(frozen=True, slots=True)
class HouseDevice:
    device_id: str
    name: str
    model: str | None
    manufacturer: str | None
    area_id: str | None


@dataclass(frozen=True, slots=True)
class HouseEntity:
    """One registry entry. `role` is `None` until the owner confirms it either way."""

    entity_id: str
    integration: str
    device_id: str | None
    area_id: str | None
    name: str | None
    role: DeviceRole | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class HouseState:
    """HA's reading of one player, passed through rather than reinterpreted."""

    entity_id: str
    state: str
    friendly_name: str | None = None
    app_id: str | None = None
    app_name: str | None = None
    media_title: str | None = None
    media_duration_seconds: float | None = None
    media_position_seconds: float | None = None
    media_position_updated_at: datetime | None = None


@dataclass(frozen=True)
class HouseSnapshot:
    """Everything HA reported at one moment.

    Each tuple is kept sorted by id, so two snapshots of one house compare
    equal however their rows arrived. Not slotted: the lookups below are
    `cached_property`, which needs an instance `__dict__`, and a new snapshot
    (every `with_*` builds one through `__init__`) starts with empty caches.
    """

    areas: tuple[HouseArea, ...] = ()
    devices: tuple[HouseDevice, ...] = ()
    entities: tuple[HouseEntity, ...] = ()
    states: tuple[HouseState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "areas", tuple(sorted(self.areas, key=lambda a: a.area_id)))
        object.__setattr__(self, "devices", tuple(sorted(self.devices, key=lambda d: d.device_id)))
        object.__setattr__(
            self, "entities", tuple(sorted(self.entities, key=lambda e: e.entity_id))
        )
        object.__setattr__(self, "states", tuple(sorted(self.states, key=lambda s: s.entity_id)))

    @cached_property
    def _areas(self) -> dict[str, HouseArea]:
        return {one.area_id: one for one in self.areas}

    @cached_property
    def _devices(self) -> dict[str, HouseDevice]:
        return {one.device_id: one for one in self.devices}

    @cached_property
    def _entities(self) -> dict[str, HouseEntity]:
        return {one.entity_id: one for one in self.entities}

    @cached_property
    def _states(self) -> dict[str, HouseState]:
        return {one.entity_id: one for one in self.states}

    def area(self, area_id: str) -> HouseArea | None:
        return self._areas.get(area_id)

    def device(self, device_id: str) -> HouseDevice | None:
        return self._devices.get(device_id)

    def entity(self, entity_id: str) -> HouseEntity | None:
        return self._entities.get(entity_id)

    def state(self, entity_id: str) -> HouseState | None:
        return self._states.get(entity_id)

    def _device_of(self, entity: HouseEntity) -> HouseDevice | None:
        return self.device(entity.device_id) if entity.device_id is not None else None

    def area_of(self, entity: HouseEntity) -> str | None:
        """The entity's own area, else its device's: HA's own precedence."""
        if entity.area_id is not None:
            return entity.area_id
        device = self._device_of(entity)
        return device.area_id if device is not None else None

    def model_of(self, entity: HouseEntity) -> str | None:
        device = self._device_of(entity)
        return device.model if device is not None else None

    def display_name(self, entity: HouseEntity) -> str:
        """What HA itself shows, falling back through the registry to the id."""
        state = self.state(entity.entity_id)
        if state is not None and state.friendly_name:
            return state.friendly_name
        if entity.name:
            return entity.name
        device = self._device_of(entity)
        return device.name if device is not None else entity.entity_id

    def with_state(self, state: HouseState) -> "HouseSnapshot":
        kept = tuple(one for one in self.states if one.entity_id != state.entity_id)
        return replace(self, states=(*kept, state))

    def without_state(self, entity_id: str) -> "HouseSnapshot":
        return replace(self, states=tuple(one for one in self.states if one.entity_id != entity_id))

    def with_registries(
        self,
        *,
        areas: tuple[HouseArea, ...] | None = None,
        devices: tuple[HouseDevice, ...] | None = None,
        entities: tuple[HouseEntity, ...] | None = None,
    ) -> "HouseSnapshot":
        return HouseSnapshot(
            areas=self.areas if areas is None else areas,
            devices=self.devices if devices is None else devices,
            entities=self.entities if entities is None else entities,
            states=self.states,
        )
