"""Reading HA's rows: the round trip, HA's own precedence, and a bad row never stopping a list."""

import ast
import inspect
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import FrameType
from typing import Any

import pytest

from tests.fakes.home_assistant_rows import LABEL_NAMES as FAKE_LABEL_NAMES
from tests.fakes.home_assistant_rows import HouseRows, device_row, recorded, rows_of
from tests.fakes.house import APPLE_TV, APPLE_TV_DEVICE, POSITION_UPDATED_AT, this_house
from usher.adapters.homeassistant import mapping
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
    pytest.param("areas", lambda rows: {**rows.areas[0], "name": ""}, id="area-name-empty"),
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


def test_an_empty_optional_text_reads_as_absent() -> None:
    row = device_row(_HOUSE.devices[0], 1)
    assert row["model"], "the premise: the row carries a model"
    assert to_device({**row, "model": ""}).model is None


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


@pytest.mark.parametrize("key", ["friendly_name", "app_id", "app_name", "media_title"])
def test_an_empty_attribute_text_reads_as_absent(key: str) -> None:
    row = _row_of(_rows().states, APPLE_TV)
    assert row["attributes"][key], "the premise: the player's row carries it"
    reading = _state({**row, "attributes": {**row["attributes"], key: ""}})
    assert getattr(reading, key) is None


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
    labels = ["usher", "kids", "favourite"]
    assert labels != sorted(labels), "the premise: the entry's order is not sorted order"
    assert labels_of_entry({"labels": labels}) == labels


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
_SUN_STATE = {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}}


@pytest.mark.parametrize(
    "data",
    [
        "not a mapping",
        {"new_state": None},
        {"entity_id": APPLE_TV, "new_state": {"entity_id": APPLE_TV, "state": "on"}},
        {"entity_id": APPLE_TV, "new_state": _OTHER_PLAYERS_STATE},
        {"entity_id": APPLE_TV, "new_state": _SUN_STATE},
    ],
    ids=[
        "not-a-mapping",
        "no-entity",
        "no-attributes",
        "another-entitys-state",
        "another-domains-state",
    ],
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
#: The paths each kind's list reader reads from `_target`'s row of that kind: the first area's,
#: the Apple TV's device's, or the Apple TV's own. `attributes.` ones are nested. The case below
#: asserts it equals what a `_Recording` sees read.
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
    "states": (
        "entity_id",
        "state",
        "attributes",
        *(f"attributes.{key}" for key in _ATTRIBUTES_READ),
    ),
}
#: The paths each reader reads from a row no list reader is handed.
_READ_ELSEWHERE: dict[str, frozenset[str]] = {
    # An `entity_registry/get` answer, read before a label write.
    "labels_of_entry": frozenset({"labels"}),
    # The label registry's rows, and a label create's answer.
    "label_ids": frozenset({"label_id"}),
    "label_id_of": frozenset({"label_id"}),
    # A `state_changed` event's data, whose new state is read as a state row.
    "state_change": frozenset(
        {"entity_id", "new_state", *(f"new_state.{path}" for path in _READ_KEYS["states"])}
    ),
}
#: Parameters of mapping.py's own functions that take no key, by function.
_NOT_KEYS: dict[str, frozenset[str]] = {
    # The list's name, for an error message.
    "_rows": frozenset({"what"}),
}
#: One spelling of each form `_keys_named` reads a key from, and one it skips.
_FORMS = """
K: Final = "by-constant"
def _rows(answer, read, what): ...
def _reader(row, key): ...
_reader(row, "by-helper")
_rows(row, read, "skipped")
row.get("by-get")
row["by-subscript"]
anything(row, key="by-keyword")
row.get(K)
"""
#: Public functions in mapping.py that read no row, each with what it takes instead.
_PURE = {
    "role_of": "an entry's labels, already read from its row",
    "labels_with_role": "an entry's labels, already read from its row, and a role",
}
#: Types `this_house` never renders at these keys, and a live HA sends.
_GAPS = {
    ("devices", "model", "NoneType"),
    ("devices", "manufacturer", "NoneType"),
    ("entities", "device_id", "NoneType"),
    ("entities", "area_id", "str"),
}


class _Recording(Mapping[str, object]):
    """A row recording the path of every key read from it, a nested mapping's as `outer.key`.

    `get` and `in` reach `__getitem__` through `Mapping`'s mixins; iterating records `*`.
    A key is recorded before it is looked up, so reading an absent one counts too.
    """

    def __init__(self, row: Mapping[str, object], read: set[str], prefix: str = "") -> None:
        self._row = row
        self._read = read
        self._prefix = prefix

    def __getitem__(self, key: str) -> object:
        self._read.add(self._prefix + key)
        value = self._row[key]
        if isinstance(value, Mapping):
            return _Recording(value, self._read, f"{self._prefix}{key}.")
        return value

    def __iter__(self) -> Iterator[str]:
        self._read.add(self._prefix + "*")
        return iter(self._row)

    def __len__(self) -> int:
        return len(self._row)


def _target(rows: HouseRows, kind: str) -> dict[str, Any]:
    """The Apple TV's row of `kind`: its device's for `devices`, the first area's for `areas`."""
    if kind == "areas":
        return rows.areas[0]
    if kind == "devices":
        return next(row for row in rows.devices if row["id"] == APPLE_TV_DEVICE)
    return _row_of(getattr(rows, kind), APPLE_TV)


def _paths_read(kind: str) -> set[str]:
    """What `kind`'s reader reads from `_target`'s row: the first area's, else the Apple TV's."""
    read: set[str] = set()
    lists: dict[str, list[object]] = {one: [] for one in _READ_KEYS}
    lists[kind] = [_Recording(_target(_rows(), kind), read)]
    registries_from_rows(lists["areas"], lists["devices"], lists["entities"])
    states_from_rows(lists["states"])
    return read


def _misread(read: Mapping[str, set[str]], listed: Mapping[str, Iterable[str]]) -> list[str]:
    """`kind: +path` for a path read but not listed, `kind: -path` for one listed but unread."""
    return sorted(
        [f"{kind}: +{path}" for kind in read for path in read[kind] - set(listed[kind])]
        + [f"{kind}: -{path}" for kind in listed for path in set(listed[kind]) - read[kind]]
    )


def _string(node: ast.expr, constants: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return constants.get(node.id) if isinstance(node, ast.Name) else None


def _key_arguments(node: ast.AST, params: Mapping[str, list[str]]) -> list[ast.expr]:
    if isinstance(node, ast.Subscript):
        return [node.slice]
    if not isinstance(node, ast.Call):
        return []
    found = [keyword.value for keyword in node.keywords if keyword.arg == "key"]
    if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return found + node.args[:1]
    if isinstance(node.func, ast.Name) and node.func.id in params:
        skipped = _NOT_KEYS.get(node.func.id, frozenset())
        given = [
            *zip(params[node.func.id], node.args, strict=False),
            *((keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg != "key"),
        ]
        found += [value for name, value in given if name not in skipped]
    return found


def _keys_named(source: str) -> list[tuple[str, int]]:
    """Each key `source` names, with its line: a string constant, or a module constant's name.

    A key is an argument to one of the module's own functions (less `_NOT_KEYS`), the first
    argument of a `.get`, a subscript, or a `key=` keyword to anything.
    """
    tree = ast.parse(source)
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Constant):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(node.value.value, str):
                value = node.value.value
                constants.update({one.id: value for one in targets if isinstance(one, ast.Name)})
    params = {
        node.name: [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    return [
        (key, argument.lineno)
        for node in ast.walk(tree)
        for argument in _key_arguments(node, params)
        if (key := _string(argument, constants)) is not None
    ]


def _functions_entered(run: Callable[[], object]) -> set[str]:
    """The names of mapping.py's functions `run` enters, off the interpreter's profile hook."""
    entered: set[str] = set()

    def profile(frame: FrameType, event: str, arg: object) -> None:
        if event == "call" and frame.f_code.co_filename == mapping.__file__:
            entered.add(frame.f_code.co_name)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        run()
    finally:
        sys.setprofile(previous)
    return entered


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
    read = {kind: _paths_read(kind) for kind in _READ_KEYS}
    assert _misread(read, _READ_KEYS) == [], "the premise: _READ_KEYS is what each reader reads"
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


def test_each_reader_beside_the_lists_reads_only_its_own_paths() -> None:
    rows = _rows()
    read: dict[str, set[str]] = {name: set() for name in _READ_ELSEWHERE}
    assert labels_of_entry(_Recording(rows.details[APPLE_TV], read["labels_of_entry"]))
    assert label_ids([_Recording(rows.labels[0], read["label_ids"])])
    assert label_id_of(_Recording(rows.labels[0], read["label_id_of"]))
    event = {"entity_id": APPLE_TV, "new_state": _row_of(rows.states, APPLE_TV)}
    changed = state_change(_Recording(event, read["state_change"]))
    assert changed is not None and changed.state is not None, "the premise: the state was read"
    assert _misread(read, _READ_ELSEWHERE) == []


def test_every_key_the_mapping_names_is_a_listed_path() -> None:
    """A read no rendered row reaches, behind a short-circuit or a branch, still names its key."""
    forms = {key for key, _ in _keys_named(_FORMS)}
    assert forms == {"by-constant", "by-helper", "by-get", "by-subscript", "by-keyword"}, (
        "the premise: the scan reads every form, and skips a `_NOT_KEYS` parameter"
    )
    listed = {
        path.rsplit(".", 1)[-1]
        for table in (_READ_KEYS, _READ_ELSEWHERE)
        for paths in table.values()
        for path in paths
    }
    named = _keys_named(inspect.getsource(mapping))
    assert sorted(listed - {key for key, _ in named}) == [], "the premise: it names every one"
    assert [f"{key} (line {line})" for key, line in named if key not in listed] == []


def test_every_public_function_that_reads_a_row_is_held_to_its_paths() -> None:
    public = {
        name
        for name, function in inspect.getmembers(mapping, inspect.isfunction)
        if function.__module__ == mapping.__name__ and not name.startswith("_")
    }
    assert set(_PURE) | set(_READ_ELSEWHERE) <= public, "the premise: each exemption is public"
    entered = _functions_entered(lambda: [_paths_read(kind) for kind in _READ_KEYS])
    assert {"registries_from_rows", "states_from_rows"} <= entered, "the premise: both lists ran"
    assert sorted(public - entered - set(_READ_ELSEWHERE) - set(_PURE)) == []
