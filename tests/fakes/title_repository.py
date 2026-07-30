"""In-memory TitleRepository, for services to be unit-tested against.

Lives outside `tests/unit/` deliberately: from M4 onward, service tests
import this the same way an adapter imports its port, and importing a test
*module* (`tests.unit.test_ports`) would drag in that module's fixtures and
parametrized tests along with it.
"""

import uuid
from datetime import UTC, datetime

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository

# Mirrors db/models/title.py's three partial unique indexes exactly, name
# for name -- this is what lets RepositoryConflict.constraint agree
# between this fake and the real, Postgres-backed repository (which reads
# its constraint name from asyncpg's own structured error fields; see
# title.py's _constraint_name). Checked in this fixed order so the fake is
# deterministic when a candidate conflicts on more than one field at once.
#
# tmdb_id's entry carries `kind_scoped=True`: its index is composite
# (tmdb_id, kind), so two rows sharing a tmdb_id across kinds do NOT
# conflict. ADR-0011.
_PROVIDER_ID_CONSTRAINTS: tuple[tuple[str, str, bool], ...] = (
    ("tmdb_id", "ix_titles_tmdb_id_kind", True),
    ("imdb_id", "ix_titles_imdb_id", False),
    ("tvdb_id", "ix_titles_tvdb_id", False),
)


def _provider_id_conflict(candidate: Title, other: Title) -> str | None:
    """The constraint name Postgres's own partial unique index would
    report for the first non-null tmdb_id, imdb_id, or tvdb_id `candidate`
    and `other` (a different row) share -- `None` if they don't conflict.

    Mirrors `db/models/title.py`'s three partial unique indexes
    (`ix_titles_tmdb_id_kind`/`ix_titles_imdb_id`/`ix_titles_tvdb_id` —
    unique only where the column `IS NOT NULL`, so many rows may share a
    null provider id) — without this, the fake would let a service add or
    update two rows onto the same TMDb/IMDb/TVDB title in a unit test,
    while the real, Postgres-backed repository rejects the identical call
    with `RepositoryConflict`. That divergence would only surface in
    production, which is exactly what a fake exists to prevent.
    """
    for field, constraint, kind_scoped in _PROVIDER_ID_CONSTRAINTS:
        value = getattr(candidate, field)
        if value is None or value != getattr(other, field):
            continue
        if kind_scoped and candidate.kind is not other.kind:
            continue
        return constraint
    return None


def _conflict(title_id: uuid.UUID, constraint: str) -> RepositoryConflict:
    """Same message shape as the real repository's title.py:_conflict --
    see that function's docstring for why it never claims `title_id`
    itself already exists."""
    return RepositoryConflict(
        f"title {title_id} conflicts with an existing title (constraint: {constraint})",
        constraint=constraint,
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
            # "pk_titles" -- the real repository's own primary key
            # constraint name (db/base.py's naming convention: "pk_%(table_name)s").
            raise _conflict(title.id, "pk_titles")
        for other in self._titles.values():
            constraint = _provider_id_conflict(title, other)
            if constraint is not None:
                raise _conflict(title.id, constraint)
        # Postgres is the authoritative clock for created_at/updated_at --
        # PostgresTitleRepository._to_row excludes both from the INSERT, so
        # the database's own server_default assigns them, never whatever
        # the caller's Title happened to carry (a stale retry, a
        # deliberately backdated import, ...). Stamping here, ignoring
        # title.created_at/title.updated_at entirely, is what makes this
        # fake agree -- verified divergence: before this, the fake
        # preserved the caller's values verbatim, including letting
        # update() overwrite created_at, which the real repository can
        # never do (see tests/contract/title_repository_contract.py's
        # test_created_at_is_not_taken_from_the_caller and
        # test_created_at_is_stable_across_updates).
        now = datetime.now(UTC)
        self._titles[title.id] = title.evolve(created_at=now, updated_at=now)

    async def update(self, title: Title) -> None:
        existing = self._titles.get(title.id)
        if existing is None:
            raise RepositoryNotFound(f"no title {title.id} to update")
        others = (t for tid, t in self._titles.items() if tid != title.id)
        for other in others:
            constraint = _provider_id_conflict(title, other)
            if constraint is not None:
                raise _conflict(title.id, constraint)
        # created_at is carried over from the persisted row, never taken
        # from the incoming title -- same reasoning as add() above. Real
        # Postgres UPDATEs simply never mention the column (title.py's
        # update() explicitly excludes it from the copy loop), so it can't
        # move after insert; updated_at, in contrast, always advances on a
        # real write (the set_updated_at trigger / onupdate=func.now()),
        # so it's re-stamped here too rather than copied from `existing`.
        self._titles[title.id] = title.evolve(
            created_at=existing.created_at, updated_at=datetime.now(UTC)
        )

    async def get(self, title_id: uuid.UUID) -> Title | None:
        return self._titles.get(title_id)

    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # Same guard, same reason, as PostgresTitleRepository.get_by_tmdb_id:
        # `title.tmdb_id == None` would match the first title with a null
        # tmdb_id instead of finding nothing, mirroring Postgres's own
        # `IS NULL` behaviour for the same comparison -- see that method's
        # comment. The `kind` filter mirrors ix_titles_tmdb_id_kind.
        if tmdb_id is None:
            return None
        for title in self._titles.values():
            if title.tmdb_id == tmdb_id and title.kind is kind:
                return title
        return None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        if imdb_id is None:
            return None
        for title in self._titles.values():
            if title.imdb_id == imdb_id:
                return title
        return None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        counts: dict[EnrichmentState, int] = dict.fromkeys(EnrichmentState, 0)
        for title in self._titles.values():
            counts[title.enrichment_state] += 1
        return counts
