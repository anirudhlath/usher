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
        return self._sources.get(source_id)

    async def list_all(self) -> list[Source]:
        return sorted(self._sources.values(), key=lambda source: source.name)

    async def delete(self, source_id: uuid.UUID) -> bool:
        return self._sources.pop(source_id, None) is not None
