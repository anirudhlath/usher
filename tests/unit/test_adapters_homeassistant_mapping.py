"""Reading HA's rows: the round trip, HA's own precedence, and a bad row never stopping a list."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.home_assistant_rows import LABEL_NAMES as FAKE_LABEL_NAMES
from tests.fakes.home_assistant_rows import HouseRows, device_row, recorded, rows_of
from tests.fakes.house import APPLE_TV, APPLE_TV_DEVICE, POSITION_UPDATED_AT, this_house
from usher.adapters.homeassistant.mapping import (
    LABEL_NAMES,
    MalformedRow,
    StateChange,
    label_id_of,
    label_ids,
    labels_of_entry,
    labels_with_role,
    registries_from_rows,
    role_of,
    state_change,
    states_from_rows,
    to_device,
    to_entity,
    to_state,
)
from usher.domain.house import DeviceRole, HouseEntity, HouseSnapshot, HouseState
from usher.ports.errors import PortDataMalformed

_HOUSE = this_house(shield=DeviceRole.IGNORE)


def _rows() -> HouseRows:
    return rows_of(_HOUSE, labels={APPLE_TV: ("favourite",)})


def _read(rows: HouseRows) -> tuple[HouseSnapshot, int]:
    house, dropped = registries_from_rows(rows.areas, rows.devices, rows.entities)
    states, dropped_states = states_from_rows(rows.states)
    return replace(house, states=states), dropped + dropped_states


def _row_of(rows: list[dict[str, Any]], entity_id: str) -> dict[str, Any]:
    return next(row for row in rows if row["entity_id"] == entity_id)


def _entity(row: object) -> HouseEntity:
    found = to_entity(row)
    assert found is not None
    return found


def _state(row: object) -> HouseState:
    found = to_state(row)
    assert found is not None
    return found


def test_the_rendered_house_reads_back_as_the_house_it_was_rendered_from() -> None:
    roles = {entity.role for entity in _HOUSE.entities}
    assert {DeviceRole.USE, DeviceRole.IGNORE, None} <= roles, "the premise: all three"
    assert _read(_rows()) == (_HOUSE, 0)


def test_a_row_from_another_domain_is_skipped_and_not_counted() -> None:
    rows = _rows()
    sensor = {**rows.entities[0], "entity_id": "sensor.hallway_temperature", "platform": "zha"}
    sun = {**rows.states[0], "entity_id": "sun.sun", "state": "above_horizon"}
    rows.entities.append(sensor)
    rows.states.append(sun)
    assert _read(rows) == (_HOUSE, 0)


_BROKEN: list[Any] = [
    pytest.param("areas", lambda rows: {**rows.areas[0], "name": None}, id="area-name-null"),
    pytest.param("areas", lambda rows: "not a row", id="area-not-a-mapping"),
    pytest.param("devices", lambda rows: {**rows.devices[0], "id": 7}, id="device-id-int"),
    pytest.param(
        "devices", lambda rows: {**rows.devices[0], "model": ["x"]}, id="device-model-list"
    ),
    pytest.param("entities", lambda rows: {**rows.entities[0], "labels": None}, id="labels-null"),
    pytest.param(
        "entities", lambda rows: {**rows.entities[0], "labels": ["usher", 3]}, id="labels-int"
    ),
    pytest.param(
        "entities",
        lambda rows: {key: value for key, value in rows.entities[0].items() if key != "platform"},
        id="entity-no-platform",
    ),
    pytest.param("states", lambda rows: {**rows.states[0], "attributes": "x"}, id="attributes-str"),
    pytest.param("states", lambda rows: {**rows.states[0], "state": None}, id="state-null"),
]


@pytest.mark.parametrize(("kind", "broken"), _BROKEN)
def test_a_malformed_row_is_dropped_and_counted_never_raised(
    kind: str, broken: Callable[[HouseRows], object]
) -> None:
    rows = _rows()
    getattr(rows, kind).append(broken(rows))
    assert _read(rows) == (_HOUSE, 1)


@pytest.mark.parametrize("answer", [None, {"result": []}, "[]"])
def test_an_answer_that_is_not_a_list_is_refused_naming_the_list(answer: object) -> None:
    with pytest.raises(PortDataMalformed, match="area list"):
        registries_from_rows(answer, [], [])
    with pytest.raises(PortDataMalformed, match="device list"):
        registries_from_rows([], answer, [])
    with pytest.raises(PortDataMalformed, match="entity list"):
        registries_from_rows([], [], answer)
    with pytest.raises(PortDataMalformed, match="state list"):
        states_from_rows(answer)


def test_the_owners_device_name_wins_over_the_integrations() -> None:
    row = {**device_row(_HOUSE.devices[0], 1), "name": "Integration", "name_by_user": "Owner"}
    assert to_device(row).name == "Owner"
    assert to_device({**row, "name_by_user": None}).name == "Integration"
    assert to_device({**row, "name_by_user": None, "name": None}).name == row["id"]


def test_an_entity_name_falls_back_to_the_integrations_original_name() -> None:
    row = {**_rows().entities[0], "name": None, "original_name": "Original"}
    assert _entity(row).name == "Original"
    assert _entity({**row, "name": "Owner"}).name == "Owner"
    assert _entity({**row, "original_name": None}).name is None


@pytest.mark.parametrize(
    ("labels", "role"),
    [
        ([], None),
        (["favourite"], None),
        (["usher"], DeviceRole.USE),
        (["usher_ignore"], DeviceRole.IGNORE),
        (["usher", "usher_ignore"], DeviceRole.IGNORE),
        (["usher_ignore", "usher"], DeviceRole.IGNORE),
    ],
)
def test_ignore_wins_and_a_foreign_label_is_no_role(
    labels: list[str], role: DeviceRole | None
) -> None:
    assert role_of(labels) is role


@pytest.mark.parametrize(
    ("disabled_by", "disabled"),
    [(None, False), ("user", True), ("integration", True), ("config_entry", True)],
)
def test_disabled_by_anyone_is_disabled(disabled_by: str | None, disabled: bool) -> None:
    assert _entity({**_rows().entities[0], "disabled_by": disabled_by}).disabled is disabled


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(489, 489.0), (489.5, 489.5), (True, None), ("489", None), (float("nan"), None)],
)
def test_a_position_is_a_finite_number_and_never_a_bool(
    value: object, seconds: float | None
) -> None:
    row = _row_of(_rows().states, APPLE_TV)
    reading = _state({**row, "attributes": {**row["attributes"], "media_position": value}})
    assert reading.media_position_seconds == seconds


@pytest.mark.parametrize(
    ("value", "instant"),
    [
        (POSITION_UPDATED_AT.isoformat(), POSITION_UPDATED_AT),
        ("2026-10-06T19:00:00", None),
        ("yesterday", None),
        (1_790_000_000, None),
    ],
)
def test_a_position_instant_is_an_aware_iso_datetime(
    value: object, instant: datetime | None
) -> None:
    assert POSITION_UPDATED_AT.tzinfo is UTC, "the premise: the aware arm is aware"
    row = _row_of(_rows().states, APPLE_TV)
    attributes = {**row["attributes"], "media_position_updated_at": value}
    assert _state({**row, "attributes": attributes}).media_position_updated_at == instant


def test_a_role_keeps_every_other_label_in_its_order() -> None:
    assert labels_with_role(["b", "usher_ignore", "a"], DeviceRole.USE) == ["b", "a", "usher"]
    assert labels_with_role(["usher", "a"], DeviceRole.IGNORE) == ["a", "usher_ignore"]
    assert labels_with_role([], DeviceRole.USE) == ["usher"]


@pytest.mark.parametrize(
    "entry", [{}, {"labels": None}, {"labels": "usher"}, {"labels": ["usher", None]}, []]
)
def test_an_entry_without_a_label_list_is_refused(entry: object) -> None:
    with pytest.raises(PortDataMalformed, match="no label list"):
        labels_of_entry(entry)


def test_an_entry_with_a_label_list_answers_it() -> None:
    assert labels_of_entry({"labels": ["favourite", "usher"]}) == ["favourite", "usher"]


def test_the_label_registry_answers_its_ids_skipping_an_odd_row() -> None:
    answer = [{"label_id": "usher", "name": "Usher"}, {"name": "no id"}, {"label_id": "kids"}]
    assert label_ids(answer) == frozenset({"usher", "kids"})
    with pytest.raises(PortDataMalformed, match="label list"):
        label_ids({"usher": "Usher"})


def test_a_created_label_answers_its_id_or_is_refused() -> None:
    assert label_id_of({"label_id": "usher", "name": "Usher"}) == "usher"
    with pytest.raises(PortDataMalformed, match="no id"):
        label_id_of({"name": "Usher"})


def test_each_label_id_is_the_slug_home_assistant_derives_from_its_name() -> None:
    """`config/label_registry/create` takes a name and derives the id; Usher checks it."""
    assert set(LABEL_NAMES) == {"usher", "usher_ignore"}
    assert {label: name.lower().replace(" ", "_") for label, name in LABEL_NAMES.items()} == {
        label: label for label in LABEL_NAMES
    }
    assert FAKE_LABEL_NAMES.items() >= LABEL_NAMES.items()


def test_a_state_change_reads_a_players_new_state() -> None:
    row = _row_of(_rows().states, APPLE_TV)
    expected = _HOUSE.state(APPLE_TV)
    assert expected is not None
    data = {"entity_id": APPLE_TV, "new_state": row, "old_state": None}
    assert state_change(data) == StateChange(APPLE_TV, expected)


def test_a_removed_player_is_a_state_change_to_nothing() -> None:
    data: dict[str, object] = {"entity_id": APPLE_TV, "new_state": None, "old_state": {}}
    assert state_change(data) == StateChange(APPLE_TV, None)


def test_another_domains_change_is_nothing_to_apply() -> None:
    assert state_change({"entity_id": "sun.sun", "new_state": {}, "old_state": None}) is None


_OTHER_PLAYERS_STATE = {"entity_id": "media_player.other", "state": "on", "attributes": {}}


@pytest.mark.parametrize(
    "data",
    [
        "not a mapping",
        {"new_state": None},
        {"entity_id": APPLE_TV, "new_state": {"entity_id": APPLE_TV, "state": "on"}},
        {"entity_id": APPLE_TV, "new_state": _OTHER_PLAYERS_STATE},
    ],
    ids=["not-a-mapping", "no-entity", "no-attributes", "another-entitys-state"],
)
def test_a_malformed_state_change_raises_for_the_caller_to_count(data: object) -> None:
    with pytest.raises(MalformedRow):
        state_change(data)


_ATTRIBUTES_READ = (
    "friendly_name",
    "app_id",
    "app_name",
    "media_title",
    "media_duration",
    "media_position",
    "media_position_updated_at",
)
#: Every key the mapping reads, by the shape's kind; `attributes.` paths are nested.
_READ_KEYS: dict[str, tuple[str, ...]] = {
    "areas": ("area_id", "name"),
    "devices": ("id", "name_by_user", "name", "model", "manufacturer", "area_id"),
    "entities": (
        "entity_id",
        "platform",
        "device_id",
        "area_id",
        "name",
        "original_name",
        "disabled_by",
        "labels",
    ),
    "states": ("entity_id", "state", *(f"attributes.{key}" for key in _ATTRIBUTES_READ)),
}
#: Types `this_house` never renders at these keys, and a live HA sends.
_GAPS = {
    ("devices", "model", "NoneType"),
    ("devices", "manufacturer", "NoneType"),
    ("entities", "device_id", "NoneType"),
    ("entities", "area_id", "str"),
}


def _target(rows: HouseRows, kind: str) -> dict[str, Any]:
    """The Apple TV's row of `kind`: its device's for `devices`, the first area's for `areas`."""
    if kind == "areas":
        return rows.areas[0]
    if kind == "devices":
        return next(row for row in rows.devices if row["id"] == APPLE_TV_DEVICE)
    return _row_of(getattr(rows, kind), APPLE_TV)


def _sample(path: str, type_name: str) -> object:
    if type_name == "str" and path == "attributes.media_position_updated_at":
        return POSITION_UPDATED_AT.isoformat()
    samples: dict[str, object] = {
        "NoneType": None,
        "str": "sample",
        "int": 1,
        "float": 1.5,
        "bool": True,
        "list": [],
        "dict": {},
    }
    return samples[type_name]


def test_every_type_the_shape_records_at_a_read_key_reads() -> None:
    """A type a live HA sends at a key the mapping reads never costs the row."""
    shape = recorded()
    unknown = [
        f"{kind}: {path}"
        for kind, paths in _READ_KEYS.items()
        for path in paths
        if path not in shape[kind]
    ]
    assert unknown == [], "the premise: every read key is a path the shape records"
    pairs = [
        (kind, path, name)
        for kind, paths in _READ_KEYS.items()
        for path in paths
        for name in shape[kind][path]
    ]
    assert set(pairs) >= _GAPS, "the premise: the types this_house never renders are exercised"
    refused: list[str] = []
    for kind, path, name in pairs:
        rows = _rows()
        *parents, key = path.split(".")
        holder: dict[str, Any] = _target(rows, kind)
        for parent in parents:
            holder = holder[parent]
        if key not in holder or type(holder[key]).__name__ != name:
            holder[key] = _sample(path, name)
        house, dropped = _read(rows)
        if dropped or len(getattr(house, kind)) != len(getattr(_HOUSE, kind)):
            refused.append(f"{kind}: {path}: {name}")
    assert refused == []
