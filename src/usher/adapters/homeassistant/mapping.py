"""Home Assistant's registry and state rows, read into the house's types.

A row that will not read raises `MalformedRow`; the list readers count it and carry on.
"""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)
from usher.domain.screens import PLAYER_DOMAIN
from usher.ports.errors import PortDataMalformed

USE_LABEL: Final = "usher"
IGNORE_LABEL: Final = "usher_ignore"
#: The names Usher creates its labels under. HA derives each id from the name.
LABEL_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {USE_LABEL: "Usher", IGNORE_LABEL: "Usher ignore"}
)
_ROLE_LABELS: Final = {DeviceRole.USE: USE_LABEL, DeviceRole.IGNORE: IGNORE_LABEL}


class MalformedRow(ValueError):
    """One row that does not read. Counted by the caller; never leaves this package."""


@dataclass(frozen=True, slots=True)
class StateChange:
    """One player's new reading; `state` is `None` when HA removed the entity."""

    entity_id: str
    state: HouseState | None


def _mapping(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise MalformedRow(type(row).__name__)
    return row


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedRow(key)
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise MalformedRow(key)
    return value or None


def _labels(row: Mapping[str, object]) -> list[str]:
    labels = row.get("labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise MalformedRow("labels")
    return list(labels)


def role_of(labels: Iterable[str]) -> DeviceRole | None:
    """`usher_ignore` wins, so a player carrying both labels is never sent to."""
    held = set(labels)
    if IGNORE_LABEL in held:
        return DeviceRole.IGNORE
    if USE_LABEL in held:
        return DeviceRole.USE
    return None


def to_area(row: object) -> HouseArea:
    found = _mapping(row)
    return HouseArea(area_id=_text(found, "area_id"), name=_text(found, "name"))


def to_device(row: object) -> HouseDevice:
    """HA's precedence: the name the owner gave a device, then the integration's, then its id."""
    found = _mapping(row)
    device_id = _text(found, "id")
    return HouseDevice(
        device_id=device_id,
        name=_optional_text(found, "name_by_user") or _optional_text(found, "name") or device_id,
        model=_optional_text(found, "model"),
        manufacturer=_optional_text(found, "manufacturer"),
        area_id=_optional_text(found, "area_id"),
    )


def to_entity(row: object) -> HouseEntity | None:
    """A player's registry entry; `None` for any other domain's."""
    found = _mapping(row)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    return HouseEntity(
        entity_id=entity_id,
        integration=_text(found, "platform"),
        device_id=_optional_text(found, "device_id"),
        area_id=_optional_text(found, "area_id"),
        name=_optional_text(found, "name") or _optional_text(found, "original_name"),
        role=role_of(_labels(found)),
        disabled=found.get("disabled_by") is not None,
    )


def _attribute_text(attributes: Mapping[str, object], key: str) -> str | None:
    value = attributes.get(key)
    return value if isinstance(value, str) and value else None


def _seconds(attributes: Mapping[str, object], key: str) -> float | None:
    value = attributes.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def _instant(attributes: Mapping[str, object], key: str) -> datetime | None:
    value = attributes.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def to_state(row: object) -> HouseState | None:
    """A player's reading; `None` for any other domain's.

    Attributes are per-integration, so one of an unexpected type reads as
    absent rather than costing the whole player.
    """
    found = _mapping(row)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    attributes = found.get("attributes")
    if not isinstance(attributes, Mapping):
        raise MalformedRow("attributes")
    return HouseState(
        entity_id=entity_id,
        state=_text(found, "state"),
        friendly_name=_attribute_text(attributes, "friendly_name"),
        app_id=_attribute_text(attributes, "app_id"),
        app_name=_attribute_text(attributes, "app_name"),
        media_title=_attribute_text(attributes, "media_title"),
        media_duration_seconds=_seconds(attributes, "media_duration"),
        media_position_seconds=_seconds(attributes, "media_position"),
        media_position_updated_at=_instant(attributes, "media_position_updated_at"),
    )


def _rows[T](
    answer: object, read: Callable[[object], T | None], what: str
) -> tuple[tuple[T, ...], int]:
    if not isinstance(answer, list):
        raise PortDataMalformed(f"Home Assistant's {what} is not a list")
    kept: list[T] = []
    dropped = 0
    for row in answer:
        try:
            found = read(row)
        except MalformedRow:
            dropped += 1
            continue
        if found is not None:
            kept.append(found)
    return tuple(kept), dropped


def registries_from_rows(
    areas: object, devices: object, entities: object
) -> tuple[HouseSnapshot, int]:
    """The three registry lists as a snapshot with no states, and how many rows were dropped."""
    read_areas, dropped_areas = _rows(areas, to_area, "area list")
    read_devices, dropped_devices = _rows(devices, to_device, "device list")
    read_entities, dropped_entities = _rows(entities, to_entity, "entity list")
    house = HouseSnapshot(areas=read_areas, devices=read_devices, entities=read_entities)
    return house, dropped_areas + dropped_devices + dropped_entities


def states_from_rows(states: object) -> tuple[tuple[HouseState, ...], int]:
    return _rows(states, to_state, "state list")


def labels_of_entry(entry: object) -> list[str]:
    """An entry's labels, read before a write that must keep every one but Usher's."""
    try:
        return _labels(_mapping(entry))
    except MalformedRow:
        raise PortDataMalformed("Home Assistant's entity entry carries no label list") from None


def labels_with_role(labels: Sequence[str], role: DeviceRole) -> list[str]:
    """Every other label in its order, then Usher's label for `role`.

    `entity_registry/update` replaces the whole list, so this is the list written.
    """
    kept = [label for label in labels if label not in _ROLE_LABELS.values()]
    return [*kept, _ROLE_LABELS[role]]


def _label_id(row: object) -> str:
    return _text(_mapping(row), "label_id")


def label_ids(answer: object) -> frozenset[str]:
    found, _ = _rows(answer, _label_id, "label list")
    return frozenset(found)


def label_id_of(created: object) -> str:
    """The id HA gave a label it just created; checked before anything carries it."""
    try:
        return _label_id(created)
    except MalformedRow:
        raise PortDataMalformed("Home Assistant answered a label create with no id") from None


def state_change(data: object) -> StateChange | None:
    """A `state_changed` event's data; `None` for another domain's. Raises `MalformedRow`."""
    found = _mapping(data)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    new_state = found.get("new_state")
    if new_state is None:
        return StateChange(entity_id, None)
    state = to_state(new_state)
    if state is None or state.entity_id != entity_id:
        raise MalformedRow("new_state")
    return StateChange(entity_id, state)
