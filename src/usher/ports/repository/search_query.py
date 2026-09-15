"""Search queries -- what a household typed, and what happened next."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime

from usher.ports.search import SearchMode, SearchSurface, SuggestTier

__all__ = [
    "SearchQueryRecord",
    "SearchQueryRepository",
]


@dataclass(frozen=True, slots=True)
class SearchQueryRecord:
    """One answered request, as `SearchService` knows it the moment it answers.

    `clicked_title_id` and `played` are not fields: neither is knowable when a
    search answers, and `SearchQueryRepository.record_outcome` writes them
    later, keyed by `id`. `id` is minted by the caller (`usher.domain.ids`), so
    a caller needing it before the row is durable already has one.

    `tier` names the `SuggestIndex` that answered a keystroke and is `None` on
    a search, which is what `surface` reads off it -- the two cannot disagree
    because there is only one of them. `mode` on a suggest row is `FULL_TEXT`,
    since both tiers are btree/GIN reads with no embed and no fusion, so every
    mode-split panel filters on `surface`.

    `result_count` and `latency_ms` are plain `int`s with no bounds; the
    repository refuses a value the column cannot hold, as `RepositoryConflict`.
    """

    id: uuid.UUID
    at: AwareDatetime
    user_id: uuid.UUID
    query: str
    mode: SearchMode
    result_count: int
    latency_ms: int
    tier: SuggestTier | None = None

    @property
    def surface(self) -> SearchSurface:
        """Which box asked -- `search_queries.surface`."""
        return SearchSurface.SEARCH if self.tier is None else SearchSurface.SUGGEST


class SearchQueryRepository(ABC):
    """`search_queries` -- one row per answered search, and what it led to."""

    @abstractmethod
    async def record(self, record: SearchQueryRecord) -> None:
        """Insert one row at query time."""

    @abstractmethod
    async def record_outcome(
        self,
        query_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        clicked_title_id: uuid.UUID | None,
        played: bool,
    ) -> None:
        """Attribute a search to what happened next."""

    @abstractmethod
    async def oldest(self) -> AwareDatetime | None:
        """`min(at)` -- when the oldest surviving row was answered, `None` for an empty table."""

    @abstractmethod
    async def prune(self, *, before: datetime, limit: int) -> int:
        """Delete up to `limit` rows answered before `before`, returning how many went."""
