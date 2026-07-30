"""Port for title persistence."""

import uuid
from abc import ABC, abstractmethod

from usher.domain.enums import EnrichmentState
from usher.domain.title import Title


class TitleRepository(ABC):
    """Persistence for canonical titles, kept behind a port so services
    depend on this ABC and never on `usher.db` directly — see ADR-0009.
    `usher.db.repositories.title.PostgresTitleRepository` is the concrete,
    SQLAlchemy-backed implementation; `api/`, the composition root,
    constructs it and injects it into services.

    Every method below shares one session-wide precondition, not just
    advice to whoever implements this port: **the session must carry no
    unflushed, invalid state when any method here is called.** A
    SQL-backed implementation's session typically autoflushes by default,
    so even a pure read can trigger a write — and once more than one
    repository shares a session (from the milestone that adds
    `MediaItemRepository`/`WatchStateRepository`), "nothing else pending is
    broken" stops being a safe assumption to make on this port's behalf.
    `PostgresTitleRepository`'s reads defensively suppress autoflush for
    exactly this reason (see its module docstring), but that is a backstop
    inside one implementation, not a substitute for callers upholding the
    precondition: a caller that leaves invalid state pending across a
    repository boundary can still surface a raw storage exception the next
    time *anything* on that session flushes it, including code this port
    doesn't own.
    """

    @abstractmethod
    async def add(self, title: Title) -> None:
        """Persist a new title.

        This is an insert, not an upsert: a duplicate `title.id` — or any
        other unique constraint the backing store enforces — raises
        `RepositoryConflict` (`usher.ports.errors`). Implementations
        translate their backing store's own conflict error (e.g.
        Postgres's `IntegrityError`) into this; callers never import a
        storage-specific exception to handle it. See `update` for
        mutating a title that already exists.

        The caller owns the session and the transaction: this flushes, so
        the row and any conflict are visible immediately, but it never
        commits. Committing or rolling back is the caller's call.
        """

    @abstractmethod
    async def update(self, title: Title) -> None:
        """Persist a mutated, already-existing title — e.g.
        `title.evolve(enrichment_state=EnrichmentState.ENRICHED, ...)`
        after enrichment, which is the read-through design's whole point
        (PRD 03: stub-on-sight, then enrich in place).

        This is an update, not an upsert: a `title.id` that does not
        already exist raises `RepositoryNotFound` (`usher.ports.errors`).
        See `add` for a brand-new title.

        Same session/transaction ownership as `add`: flushes, never
        commits.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> Title | None:
        """Fetch by Usher's own id, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_tmdb_id(self, tmdb_id: int) -> Title | None:
        """Fetch by TMDb id, or None if no title carries it."""

    @abstractmethod
    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        """Fetch by IMDb id, or None if no title carries it."""

    @abstractmethod
    async def count_by_state(self) -> dict[EnrichmentState, int]:
        """Catalog size broken down by enrichment tier.

        Always returns all three `EnrichmentState` members as keys, 0 for
        any tier with no titles — never a sparse dict. A `GROUP BY` only
        returns tiers with at least one row; an implementation must fill
        in the rest itself rather than let the query's own sparsity leak
        through (a bare `counts[EnrichmentState.ENRICHED]` must never
        raise `KeyError` just because nothing is enriched yet).
        """
