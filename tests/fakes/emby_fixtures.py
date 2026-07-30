"""Loader for the committed Emby payload fixtures.

Shape-recorded, value-synthetic: field names, nesting, and types were
transcribed from real Emby 4.9.5.0 responses; every value is invented. See
`usher.adapters.emby.mapping`'s module docstring for why a real capture is
not committed.
"""

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
