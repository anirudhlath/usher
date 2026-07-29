"""In-memory TitleRepository, for services to be unit-tested against.

Lives outside `tests/unit/` deliberately: from M4 onward, service tests
import this the same way an adapter imports its port, and importing a test
*module* (`tests.unit.test_ports`) would drag in that module's fixtures and
parametrized tests along with it.
"""

import uuid

from usher.domain.enums import EnrichmentState
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


def _provider_id_conflict(candidate: Title, other: Title) -> bool:
    """True if `candidate` and `other` (a different row) claim the same
    non-null tmdb_id, imdb_id, or tvdb_id.

    Mirrors `db/models/title.py`'s three partial unique indexes
    (`ix_titles_tmdb_id`/`ix_titles_imdb_id`/`ix_titles_tvdb_id` — unique
    only where the column `IS NOT NULL`, so many rows may share a null
    provider id) — without this, the fake would let a service add or
    update two rows onto the same TMDb/IMDb/TVDB title in a unit test,
    while the real, Postgres-backed repository rejects the identical call
    with `RepositoryConflict`. That divergence would only surface in
    production, which is exactly what a fake exists to prevent.
    """
    return (
        (candidate.tmdb_id is not None and candidate.tmdb_id == other.tmdb_id)
        or (candidate.imdb_id is not None and candidate.imdb_id == other.imdb_id)
        or (candidate.tvdb_id is not None and candidate.tvdb_id == other.tvdb_id)
    )


class FakeTitleRepository(TitleRepository):
    """Keyed the same way the real Postgres-backed
    `PostgresTitleRepository` (Task 10) is: by id, with tmdb_id and
    imdb_id as secondary lookups. `add`/`update` mirror the real
    insert-only/update-only split documented on `TitleRepository` — a
    duplicate `add` raises `RepositoryConflict` and a missing-id `update`
    raises `RepositoryNotFound`, the same exceptions the real,
    Postgres-backed repository raises (translated from `IntegrityError`
    and a missing row respectively). A fake that raised anything else
    would defeat the point: a service unit-tested against this one must
    see the same failure shape it would see in production. That includes
    a duplicate tmdb_id/imdb_id/tvdb_id under a *different* id — the real
    repository's unique partial indexes reject that too (see
    `_provider_id_conflict`), so this fake does the same.
    """

    def __init__(self) -> None:
        self._titles: dict[uuid.UUID, Title] = {}

    async def add(self, title: Title) -> None:
        if title.id in self._titles:
            raise RepositoryConflict(f"title {title.id} already exists")
        if any(_provider_id_conflict(title, other) for other in self._titles.values()):
            raise RepositoryConflict(f"title {title.id} conflicts with an existing title")
        self._titles[title.id] = title

    async def update(self, title: Title) -> None:
        if title.id not in self._titles:
            raise RepositoryNotFound(f"no title {title.id} to update")
        others = (t for tid, t in self._titles.items() if tid != title.id)
        if any(_provider_id_conflict(title, other) for other in others):
            raise RepositoryConflict(f"title {title.id} conflicts with an existing title")
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
