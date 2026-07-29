"""In-memory TitleRepository, for services to be unit-tested against.

Lives outside `tests/unit/` deliberately: from M4 onward, service tests
import this the same way an adapter imports its port, and importing a test
*module* (`tests.unit.test_ports`) would drag in that module's fixtures and
parametrized tests along with it.
"""

import uuid

from usher.domain.enums import EnrichmentState
from usher.domain.title import Title
from usher.ports.repository import TitleRepository


class FakeTitleRepository(TitleRepository):
    """Keyed the same way the real Postgres-backed
    `PostgresTitleRepository` (Task 10) is: by id, with tmdb_id and
    imdb_id as secondary lookups. `add`/`update` mirror the real
    insert-only/update-only split documented on `TitleRepository` — a
    duplicate `add` or a missing-id `update` raises, it does not silently
    overwrite or no-op.
    """

    def __init__(self) -> None:
        self._titles: dict[uuid.UUID, Title] = {}

    async def add(self, title: Title) -> None:
        if title.id in self._titles:
            raise ValueError(f"title {title.id} already exists")
        self._titles[title.id] = title

    async def update(self, title: Title) -> None:
        if title.id not in self._titles:
            raise ValueError(f"no title {title.id} to update")
        self._titles[title.id] = title

    async def get(self, title_id: uuid.UUID) -> Title | None:
        return self._titles.get(title_id)

    async def get_by_tmdb_id(self, tmdb_id: int) -> Title | None:
        for title in self._titles.values():
            if title.tmdb_id == tmdb_id:
                return title
        return None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        for title in self._titles.values():
            if title.imdb_id == imdb_id:
                return title
        return None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        counts: dict[EnrichmentState, int] = dict.fromkeys(EnrichmentState, 0)
        for title in self._titles.values():
            counts[title.enrichment_state] += 1
        return counts
