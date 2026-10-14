"""A `HouseSnapshot` rendered as Home Assistant's websocket rows, every recorded key present.

Values are invented: MACs under 02:00:00 (locally administered), hosts in 192.0.2.0/24.
"""

import importlib.util
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)

_ROOT: Final = Path(__file__).resolve().parents[2]
SHAPE: Final = _ROOT / "tests" / "fixtures" / "homeassistant" / "shape.json"
_SCRIPT: Final = _ROOT / "scripts" / "capture_ha_shape.py"
_CREATED: Final = 1_790_000_000.0
_MODIFIED: Final = 1_790_000_600.0
_CHANGED: Final = "2026-10-06T19:00:00.000000+00:00"
_ROLE_LABELS: Final = {DeviceRole.USE: "usher", DeviceRole.IGNORE: "usher_ignore"}
LABEL_NAMES: Final = {"usher": "Usher", "usher_ignore": "Usher ignore", "favourite": "Favourite"}


@cache
def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usher_ops_capture_ha_shape", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recorded() -> dict[str, dict[str, list[str]]]:
    shape: dict[str, dict[str, list[str]]] = json.loads(SHAPE.read_text())
    return shape


def unrecorded_paths(kind: str, rows: Sequence[object]) -> list[str]:
    """Every `path: type` in `rows` the recorded shape of `kind` never saw."""
    script = _script()
    seen: dict[str, list[str]] = script.merged(rows, script.DEPTHS[kind])
    shape = recorded()[kind]
    return [
        f"{path}: {name}"
        for path, names in seen.items()
        for name in names
        if name not in shape.get(path, ())
    ]


def missing_keys(kind: str, rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Every recorded top-level key of `kind` some row lacks, as `index: key`."""
    top = {path for path in recorded()[kind] if "." not in path and "[" not in path}
    return [f"{index}: {key}" for index, row in enumerate(rows) for key in sorted(top - row.keys())]


def area_row(area: HouseArea) -> dict[str, Any]:
    return {
        "aliases": [],
        "area_id": area.area_id,
        "created_at": _CREATED,
        "floor_id": None,
        "humidity_entity_id": None,
        "icon": None,
        "labels": [],
        "modified_at": _MODIFIED,
        "name": area.name,
        "picture": None,
        "temperature_entity_id": None,
    }


def device_row(device: HouseDevice, index: int) -> dict[str, Any]:
    entry = f"c{index:031d}"
    return {
        "area_id": device.area_id,
        "config_entries": [entry],
        "config_entries_subentries": {entry: [None]},
        "configuration_url": f"http://192.0.2.{index}/",
        "connections": [["mac", f"02:00:00:00:00:{index:02x}"]],
        "created_at": _CREATED,
        "disabled_by": None,
        "entry_type": None,
        "hw_version": None,
        "id": device.device_id,
        "identifiers": [["synthetic", f"device-{index:04d}"]],
        "labels": [],
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_id": None,
        "modified_at": _MODIFIED,
        "name": device.name,
        "name_by_user": None,
        "primary_config_entry": entry,
        "serial_number": None,
        "sw_version": None,
        "via_device_id": None,
    }


def entity_row(entity: HouseEntity, index: int, *, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """`labels` are the entry's others; Usher's own follows from `entity.role`."""
    own = () if entity.role is None else (_ROLE_LABELS[entity.role],)
    return {
        "area_id": entity.area_id,
        "categories": {},
        "config_entry_id": None,
        "config_subentry_id": None,
        "created_at": _CREATED,
        "device_id": entity.device_id,
        "disabled_by": "user" if entity.disabled else None,
        "entity_category": None,
        "entity_id": entity.entity_id,
        "has_entity_name": True,
        "hidden_by": None,
        "icon": None,
        "id": f"{index:032x}",
        "labels": [*labels, *own],
        "modified_at": _MODIFIED,
        "name": entity.name,
        "options": {},
        "original_name": None,
        "platform": entity.integration,
        "translation_key": None,
        "unique_id": f"synthetic-{index:04d}",
    }


def entity_detail_row(
    entity: HouseEntity, index: int, *, labels: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`config/entity_registry/get`'s answer: the list row plus five keys."""
    return {
        **entity_row(entity, index, labels=labels),
        "aliases": [],
        "capabilities": {},
        "device_class": None,
        "original_device_class": None,
        "original_icon": None,
    }


def _number(value: float | None) -> int | float | None:
    """HA sends a whole number of seconds as an `int`."""
    return int(value) if value is not None and float(value).is_integer() else value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def state_row(state: HouseState) -> dict[str, Any]:
    attributes: dict[str, Any] = {"supported_features": 0}
    for key, value in (
        ("friendly_name", state.friendly_name),
        ("app_id", state.app_id),
        ("app_name", state.app_name),
        ("media_title", state.media_title),
        ("media_duration", _number(state.media_duration_seconds)),
        ("media_position", _number(state.media_position_seconds)),
        ("media_position_updated_at", _iso(state.media_position_updated_at)),
    ):
        if value is not None:
            attributes[key] = value
    return {
        "attributes": attributes,
        "context": {"id": "01K0000000000000000000000S", "parent_id": None, "user_id": None},
        "entity_id": state.entity_id,
        "last_changed": _CHANGED,
        "last_reported": _CHANGED,
        "last_updated": _CHANGED,
        "state": state.state,
    }


def label_row(label_id: str, name: str) -> dict[str, Any]:
    """HA core's label keys, as the shape records them."""
    return {
        "color": None,
        "created_at": _CREATED,
        "description": None,
        "icon": None,
        "label_id": label_id,
        "modified_at": _MODIFIED,
        "name": name,
    }


@dataclass
class HouseRows:
    areas: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    labels: list[dict[str, Any]]
    states: list[dict[str, Any]]
    details: dict[str, dict[str, Any]]


def rows_of(
    house: HouseSnapshot, *, labels: Mapping[str, tuple[str, ...]] | None = None
) -> HouseRows:
    """The house as HA would list it; the label registry holds every label in use."""
    others = labels or {}
    entities = [
        entity_row(entity, index, labels=others.get(entity.entity_id, ()))
        for index, entity in enumerate(house.entities, start=1)
    ]
    in_use = sorted({label for row in entities for label in row["labels"]})
    return HouseRows(
        areas=[area_row(area) for area in house.areas],
        devices=[device_row(device, index) for index, device in enumerate(house.devices, start=1)],
        entities=entities,
        labels=[label_row(label, LABEL_NAMES.get(label, label.capitalize())) for label in in_use],
        states=[state_row(state) for state in house.states],
        details={
            entity.entity_id: entity_detail_row(
                entity, index, labels=others.get(entity.entity_id, ())
            )
            for index, entity in enumerate(house.entities, start=1)
        },
    )
