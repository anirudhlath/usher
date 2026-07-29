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
    """

    @abstractmethod
    async def add(self, title: Title) -> None:
        """Persist a new title."""

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
        """Catalog size broken down by enrichment tier."""
