"""Port for the search index."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchHit:
    title_id: uuid.UUID
    score: float


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 20
    semantic: bool = False
    filters: dict[str, Any] = field(default_factory=dict)


class SearchIndex(ABC):
    """Candidate generation. Ranking blends happen in application code, so
    this returns hits and scores, not final ordering."""

    @abstractmethod
    async def index(self, title_id: uuid.UUID) -> None:
        """Insert or update one title's document."""

    @abstractmethod
    async def remove(self, title_id: uuid.UUID) -> None:
        """Drop a title from the index."""

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SearchHit]:
        """Full-text, semantic, or fused search."""

    @abstractmethod
    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        """Typo-tolerant type-ahead over names."""
