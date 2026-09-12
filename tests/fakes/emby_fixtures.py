"""Loader for the committed Emby payload fixtures."""

import json
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
