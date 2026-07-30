"""Port for the search index."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class SearchHit:
    title_id: uuid.UUID
    score: float


class SearchMode(StrEnum):
    """`SearchRequest.mode`'s three reachable values. Reciprocal Rank
    Fusion is the design (ADR-0002), not a hypothetical option alongside a
    bool — which is why this replaced a `semantic: bool` that could not
    express `FUSED` at all."""

    FULL_TEXT = "full_text"
    SEMANTIC = "semantic"
    FUSED = "fused"


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 20
    mode: SearchMode = SearchMode.FULL_TEXT
    filters: dict[str, Any] = field(default_factory=dict)


class SearchIndex(ABC):
    """Candidate generation. Ranking blends happen in application code, so
    this returns hits and scores, not final ordering.

    🔶 Provisional — this shape is closer to Postgres's own operations
    than a neutral candidate-generation contract: `index(title_id)` forces
    a Meilisearch implementation to fetch each title back out to build its
    document (1.3M round-trips on a full rebuild); `SearchRequest.filters`
    has no key vocabulary, so two backends would invent different ones;
    there is no `index_many`/`rebuild` for bulk operations; and semantic
    search needs the query *vector* itself, which ADR-0002 already
    anticipates supplying to Meilisearch as `userProvided`. Settle if and
    when the Meilisearch gate in PRD 05 actually trips, in M6.
    """

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
        """Typo-tolerant type-ahead over names.

        🔶 Provisional — PRD 05 treats autocomplete as "a separate, narrow
        path" and ADR-0002 gates Meilisearch "for the instant-search box
        only", which suggests the real swap boundary may be this one
        method, not the whole `SearchIndex` class. Whether `suggest`
        should be its own `SuggestIndex` port is undecided; settle in M6.
        """
