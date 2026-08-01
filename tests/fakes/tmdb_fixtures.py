"""Loader for the committed TMDb payload fixtures.

Shape-recorded, value-synthetic: field names, nesting and types were
transcribed from TMDb's published API documentation on 2026-07-31; every
human-readable value is invented. See `tests/fixtures/tmdb/README.md` for
which endpoint each file records and why a real capture is not committed.
"""

import json
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "tmdb"


def load_tmdb_fixture(name: str) -> dict[str, Any]:
    """One fixture, freshly parsed on every call.

    Freshly, not cached: callers mutate what they get back -- a mock
    transport composes a series payload out of the detail and season
    fixtures -- and a shared, cached dict would let one test's mutation leak
    into the next. Same reasoning as `load_emby_fixture`.
    """
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload
