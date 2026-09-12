"""In-memory `RawPayloadStore`."""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.ports.repository import CachedPayload, RawPayloadStore

_Key = tuple[str, str, str]


class FakeRawPayloadStore(RawPayloadStore):
    def __init__(self) -> None:
        self._entries: dict[_Key, tuple[uuid.UUID, dict[str, Any], datetime]] = {}
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
        return copy.deepcopy(found[1]), found[2]

    async def put(self, provider: str, kind: str, reference: str, payload: dict[str, Any]) -> None:
        key = (provider, kind, reference)
        stored = self._entries.get(key)
        # The id already under this key, never a fresh one. `_PUT`'s
        # `DO UPDATE SET` names `payload` and `fetched_at` and not `id`, so a
        # refreshed row keeps its place in `iterate`'s order; re-minting here
        # would sort it to the end of the walk and make the walk both revisit
        # and skip.
        self._entries[key] = (
            stored[0] if stored is not None else new_id(),
            copy.deepcopy(payload),
            self._stamp(),
        )

    async def oldest_fetched_at(self, provider: str) -> AwareDatetime | None:
        stamps = [
            stamp for (name, _, _), (_, _, stamp) in self._entries.items() if name == provider
        ]
        # `min`, not `max`: the question is "how close is the oldest entry to
        # TMDb's six-month ceiling", and `max` reports perfect compliance
        # right up to the moment it is audited.
        return min(stamps) if stamps else None

    async def count(self, provider: str) -> int:
        return sum(1 for (name, _, _) in self._entries if name == provider)

    async def iterate(
        self, provider: str, *, limit: int = 500, after: uuid.UUID | None = None
    ) -> list[CachedPayload]:
        rows = [
            CachedPayload(
                id=stored_id,
                kind=kind,
                reference=reference,
                payload=copy.deepcopy(payload),
                fetched_at=stamp,
            )
            for (name, kind, reference), (stored_id, payload, stamp) in self._entries.items()
            if name == provider
        ]
        # **Actually sorted**, rather than leaning on dict insertion order.
        rows.sort(key=lambda row: row.id)
        if after is not None:
            rows = [row for row in rows if row.id > after]
        return rows[: max(limit, 0)]

    def _stamp(self) -> datetime:
        now = datetime.now(UTC)
        if self._last is not None and now <= self._last:
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now
