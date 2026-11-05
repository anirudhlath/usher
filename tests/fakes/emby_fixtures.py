"""Loader for the committed Emby payload fixtures."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "emby"


def load_emby_fixture(name: str) -> dict[str, Any]:
    """One fixture, freshly parsed on every call.

    Freshly, not cached: callers mutate what they get back -- the fake
    server overwrites fields to render a seeded item -- and a shared,
    cached dict would let one test's mutation leak into the next.
    """
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def own_entry_only(read: dict[str, Any]) -> dict[str, Any]:
    """A copy of `read` whose `MediaSources` keeps only the entry whose `ItemId` is its `Id`."""
    alone = deepcopy(read)
    alone["MediaSources"] = [
        entry for entry in alone["MediaSources"] if entry.get("ItemId") == read["Id"]
    ]
    return alone
