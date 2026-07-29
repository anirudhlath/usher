"""Port for external metadata providers."""

import uuid
from abc import ABC, abstractmethod
from typing import Any

from usher.domain.title import Title


class MetadataProvider(ABC):
    """Supplies high-quality metadata for a canonical Title."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, recorded in field provenance."""

    @abstractmethod
    async def search(self, name: str, year: int | None) -> list[dict[str, Any]]:
        """Candidate matches for a name and optional year."""

    @abstractmethod
    async def fetch(self, provider_id: int, kind: str) -> dict[str, Any]:
        """Full raw payload for one item. Stored before normalisation."""

    @abstractmethod
    def to_title(self, payload: dict[str, Any], title_id: uuid.UUID) -> Title:
        """Normalise a raw payload into a canonical Title."""

    @abstractmethod
    async def changed_since(self, days: int) -> list[int]:
        """Provider ids mutated in the window, for incremental refresh."""
