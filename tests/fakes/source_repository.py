"""In-memory SourceRepository.

Stamps `created_at`/`updated_at` itself rather than honouring the caller's,
because Postgres does -- the same divergence the title fake had to be
corrected for, where "the fake preserved caller timestamps and the real
repository never did" made a round-trip assertion pass against the fake
alone.
"""

import uuid
from datetime import UTC, datetime

from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository


class FakeSourceRepository(SourceRepository):
    def __init__(self) -> None:
        self._sources: dict[uuid.UUID, Source] = {}
        self.calls = 0

    def reset_calls(self) -> None:
        """A test-double affordance, not a port method -- the same shape
        `FakeMediaItemRepository.reset_calls` is, and here for the same
        reason: "one batched read serves every availability badge" is a
        round-trip property, and the response is byte-identical whether the
        names came from one `list_all` or from a `get` per copy. Only a
        counter can tell them apart."""
        self.calls = 0

    async def add(self, source: Source) -> None:
        if source.id in self._sources:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint="pk_sources"
            )
        now = datetime.now(UTC)
        self._sources[source.id] = source.evolve(created_at=now, updated_at=now)

    async def update(self, source: Source) -> None:
        existing = self._sources.get(source.id)
        if existing is None:
            raise RepositoryNotFound(f"source {source.id} does not exist")
        self._sources[source.id] = source.evolve(
            created_at=existing.created_at, updated_at=datetime.now(UTC)
        )

    async def get(self, source_id: uuid.UUID) -> Source | None:
        self.calls += 1
        return self._sources.get(source_id)

    async def list_all(self) -> list[Source]:
        self.calls += 1
        return sorted(self._sources.values(), key=lambda source: source.name)

    async def delete(self, source_id: uuid.UUID) -> bool:
        return self._sources.pop(source_id, None) is not None
