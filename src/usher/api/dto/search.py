"""`GET /search` and `GET /search/suggest`.

PRD 05's read path on the wire, and PRD 07's `### Screens`.
"""

import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from usher.domain.enums import TitleKind
from usher.domain.search import SearchResult
from usher.ports.search import SearchMode, SuggestTier
from usher.services.search import SearchAnswer


class SearchResultResponse(BaseModel):
    """One ranked hit, hydrated.

    `score` is the **blended** score `SearchService._blend` produced, not the
    index's — comparable within one answer and meaningless between two, because
    the relevance term is derived from position within the candidate set this
    request returned. Named `score` rather than `relevance` for exactly that
    reason, following the domain model it renders.

    `owned` rides along because PRD 05 requires unowned results to be surfaced
    "clearly marked": a client that had to ask a second question to render the
    badge would either ask it per row or not render it.

    `popularity` is nullable and stays nullable -- it is `null` for every title
    TMDb's daily export has never described, most of a fully bootstrapped catalog
    and **all** of an IMDb-only one. `popularity or 0.0` here would render
    "nobody has rated this" identically to "rated, and unpopular".
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    popularity: float | None
    owned: bool
    score: float

    @classmethod
    def of(cls, result: SearchResult) -> "SearchResultResponse":
        return cls(
            title_id=result.title_id,
            kind=result.kind,
            name=result.name,
            year=result.year,
            popularity=result.popularity,
            owned=result.owned,
            score=result.score,
        )


class SearchResponse(BaseModel):
    """The ranked results, plus what actually ran."""

    query: str
    requested_mode: SearchMode
    mode: SearchMode
    semantic_coverage: float
    expanded_query: str | None
    search_id: uuid.UUID | None
    results: tuple[SearchResultResponse, ...]

    @classmethod
    def of(cls, query: str, answer: SearchAnswer) -> "SearchResponse":
        return cls(
            query=query,
            requested_mode=answer.requested_mode,
            mode=answer.mode,
            semantic_coverage=answer.semantic_coverage,
            expanded_query=answer.expanded_query,
            search_id=answer.search_id,
            results=tuple(SearchResultResponse.of(result) for result in answer.results),
        )


class SuggestResultResponse(BaseModel):
    """One type-ahead candidate, hydrated."""

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    popularity: float | None
    owned: bool
    score: float

    @classmethod
    def of(cls, result: SearchResult) -> "SuggestResultResponse":
        return cls(
            title_id=result.title_id,
            kind=result.kind,
            name=result.name,
            year=result.year,
            popularity=result.popularity,
            owned=result.owned,
            score=result.score,
        )


class SuggestResponse(BaseModel):
    """The type-ahead box, plus which tier filled it and what it refuses."""

    query: str
    tier: SuggestTier
    min_query_length: int
    results: tuple[SuggestResultResponse, ...]

    @classmethod
    def of(
        cls,
        query: str,
        *,
        tier: SuggestTier,
        min_query_length: int,
        results: Sequence[SearchResult] = (),
    ) -> "SuggestResponse":
        return cls(
            query=query,
            tier=tier,
            min_query_length=min_query_length,
            results=tuple(SuggestResultResponse.of(result) for result in results),
        )


__all__ = [
    "SearchResponse",
    "SearchResultResponse",
    "SuggestResponse",
    "SuggestResultResponse",
]
