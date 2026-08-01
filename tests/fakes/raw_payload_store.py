"""In-memory `RawPayloadStore`.

**Where this is more forgiving than Postgres, on purpose.** Four places, each
of which the paired `tests/integration/test_raw_payload_store.py` run is what
actually closes:

- **It stores Python objects, not JSONB.** Postgres's `jsonb` is a
  normalising representation: it drops object key order, collapses duplicate
  keys, and renders every numeric as `numeric`, so a payload does not
  necessarily come back `==` to what went in. This fake `deepcopy`s and hands
  back exactly what it was given, so it can never catch a type that survives
  Python and not JSON.
- **No unique constraint on `(provider, kind, reference)`** -- it is a dict
  key, so a collision is structurally impossible rather than rejected. The
  real one needs `ON CONFLICT` naming that constraint.
- **The clock is nudged, not real.** `fetched_at` uses `datetime.now(UTC)`
  and is forced strictly forward if two writes land in the same microsecond,
  so `test_a_refresh_moves_fetched_at` can never flake here. Postgres's
  `clock_timestamp()` advances on its own -- and the real trap it guards
  against, an upsert that leaves `fetched_at` out of its `DO UPDATE SET` and
  keeps a six-month-old timestamp on a payload fetched this morning, is a
  mistake this file has no way to make.
- **No transaction**, so nothing here can leave a session poisoned.
"""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import AwareDatetime

from usher.ports.repository import RawPayloadStore

_Key = tuple[str, str, str]


class FakeRawPayloadStore(RawPayloadStore):
    def __init__(self) -> None:
        self._entries: dict[_Key, tuple[dict[str, Any], datetime]] = {}
        self._last: datetime | None = None

    async def get(
        self, provider: str, kind: str, reference: str
    ) -> tuple[dict[str, Any], AwareDatetime] | None:
        found = self._entries.get((provider, kind, reference))
        if found is None:
            return None
        # A copy on the way out as well as in: Postgres deserialises a fresh
        # object per read, so a caller that mutates what it got back cannot
        # corrupt the cache there and must not be able to here either.
        return copy.deepcopy(found[0]), found[1]

    async def put(self, provider: str, kind: str, reference: str, payload: dict[str, Any]) -> None:
        self._entries[(provider, kind, reference)] = (copy.deepcopy(payload), self._stamp())

    async def oldest_fetched_at(self, provider: str) -> AwareDatetime | None:
        stamps = [stamp for (name, _, _), (_, stamp) in self._entries.items() if name == provider]
        # `min`, not `max`: the question is "how close is the oldest entry to
        # TMDb's six-month ceiling", and `max` reports perfect compliance
        # right up to the moment it is audited.
        return min(stamps) if stamps else None

    def _stamp(self) -> datetime:
        now = datetime.now(UTC)
        if self._last is not None and now <= self._last:
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now
