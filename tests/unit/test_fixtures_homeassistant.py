"""The fake's rows held to the shapes a live Home Assistant answered with, both directions."""

import ipaddress
import re
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.home_assistant_rows import (
    HouseRows,
    missing_keys,
    recorded,
    rows_of,
    unrecorded_paths,
)
from tests.fakes.house import APPLE_TV, this_house
from usher.domain.house import DeviceRole

_ROWS = rows_of(this_house(shield=DeviceRole.IGNORE), labels={APPLE_TV: ("favourite",)})
_RECORDED_KINDS = ("areas", "devices", "entities", "entity", "labels", "states")
_TYPE_NAMES = {"NoneType", "bool", "dict", "float", "int", "list", "str"}
_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
#: A recorded key is a name; a ULID or a hex id as a segment is a value that leaked.
_SEGMENT = re.compile(r"^[a-z][a-z_]*(\[\])*$")
_ROOT = Path(__file__).resolve().parents[2]
_COMMITTED = (
    "scripts/capture_ha_shape.py",
    "tests/fakes/home_assistant.py",
    "tests/fakes/home_assistant_rows.py",
    "tests/fakes/house.py",
    "tests/fixtures/homeassistant/shape.json",
    "tests/unit/test_fixtures_homeassistant.py",
)
_HA_NAMES = re.compile(r"capture_ha_shape|home_assistant_rows")
_README = "tests/fixtures/README.md"
_DOCUMENTATION = ipaddress.ip_network("192.0.2.0/24")


def _undocumented(found: str) -> bool:
    """Outside 192.0.2.0/24, or no address at all (an octet past 255): either way, named."""
    try:
        return ipaddress.ip_address(found) not in _DOCUMENTATION
    except ValueError:
        return True


def _touched_by_the_ha_work() -> set[str]:
    """Derived, not listed: the HA fixtures, every fake or script naming the HA work, this file."""
    found = {path for path in (_ROOT / "tests/fixtures/homeassistant").rglob("*") if path.is_file()}
    for folder in ("tests/fakes", "scripts"):
        found |= {
            path for path in (_ROOT / folder).rglob("*.py") if _HA_NAMES.search(path.read_text())
        }
    found.add(Path(__file__).resolve())
    return {path.relative_to(_ROOT).as_posix() for path in found}


def _rows(kind: str, rows: HouseRows = _ROWS) -> list[dict[str, Any]]:
    return list(rows.details.values()) if kind == "entity" else list(getattr(rows, kind))


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize("kind", _RECORDED_KINDS)
def test_every_rendered_path_and_type_was_recorded(kind: str) -> None:
    assert _rows(kind), "the premise: there is something to check"
    assert unrecorded_paths(kind, _rows(kind)) == []


@pytest.mark.parametrize("kind", _RECORDED_KINDS)
def test_every_recorded_key_is_rendered(kind: str) -> None:
    assert _rows(kind), "the premise: there are rows to hold to the keys"
    top = [path for path in recorded()[kind] if "." not in path and "[" not in path]
    assert top, "the premise: the shape records a top-level key"
    assert missing_keys(kind, _rows(kind)) == []


def test_the_rows_carry_both_roles_a_foreign_label_and_a_player_with_no_area() -> None:
    labels = {label for row in _ROWS.entities for label in row["labels"]}
    assert {"favourite", "usher", "usher_ignore"} <= labels
    assert {type(row["area_id"]) for row in _ROWS.devices} == {str, type(None)}
    attributes = {key for row in _ROWS.states for key in row["attributes"]}
    assert {"media_title", "media_position", "media_position_updated_at"} <= attributes


def test_the_recorded_shape_holds_type_names_and_no_entity() -> None:
    shape = recorded()
    assert set(shape) >= {*_RECORDED_KINDS, "auth_required", "auth_ok", "result", "refusal"}
    names = {name for kind in shape.values() for found in kind.values() for name in found}
    assert names <= _TYPE_NAMES
    assert [path for kind in shape.values() for path in kind if "media_player." in path] == []
    unnamed = [
        f"{kind}: {path}"
        for kind, found in shape.items()
        for path in found
        if not all(_SEGMENT.fullmatch(segment) for segment in path.split("."))
    ]
    assert unnamed == []


def test_every_mac_and_address_in_the_rows_is_reserved_for_documentation() -> None:
    strings = list(_strings(asdict(_ROWS)))
    macs = [found for text in strings for found in _MAC.findall(text)]
    addresses = [found for text in strings for found in _IPV4.findall(text)]
    assert macs and addresses, "the premise: the rows carry both"
    assert [mac for mac in macs if not mac.lower().startswith("02:00:00:")] == []
    assert [ip for ip in addresses if _undocumented(ip)] == []


def _readme_section() -> str:
    text = (_ROOT / _README).read_text()
    start = text.index("## Home Assistant\n")
    return text[start : text.index("\n## ", start)]


def test_every_mac_and_address_in_the_committed_files_is_reserved_for_documentation() -> None:
    unscanned = sorted(_touched_by_the_ha_work() ^ set(_COMMITTED))
    assert unscanned == [], "the premise: the scan reads exactly the files the HA work touches"
    texts = {name: (_ROOT / name).read_text() for name in _COMMITTED}
    texts[f"{_README} ## Home Assistant"] = _readme_section()
    empty = [name for name, text in texts.items() if not text.strip()]
    assert empty == [], "the premise: every file and the section were read and have text"
    macs = [(name, mac) for name, text in texts.items() for mac in _MAC.findall(text)]
    addresses = [(name, ip) for name, text in texts.items() for ip in _IPV4.findall(text)]
    assert addresses, "the premise: the scan finds the documentation network the files name"
    assert [f"{name}: {mac}" for name, mac in macs if not mac.startswith("02:00:00:")] == []
    assert [f"{name}: {ip}" for name, ip in addresses if _undocumented(ip)] == []
