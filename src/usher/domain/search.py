"""What a search and a "more like this" hand back, once ranked.

`domain/` imports nothing -- not `ports/`, which imports *it*. So these carry
no `SearchMode`, no `SearchHit` and no engine vocabulary: they are what a row
renders and what a CLI prints. The service-side envelope that *does* carry a
`SearchMode` lives in `services/search.py`, beside the service, exactly as
`TitleDetail` lives beside `TitleReadService`.
"""

import uuid

from usher.domain.base import DomainModel
from usher.domain.enums import TitleKind


class SearchResult(DomainModel):
    """One ranked hit, hydrated.

    `score` is the **blended** score, not the index's -- it is comparable
    within one answer and meaningless between two, because the relevance term
    is derived from position within the candidate set that request returned.
    Named `score` rather than `relevance` for exactly that reason.

    `owned` rides along because PRD 05 requires unowned results to be surfaced
    "clearly marked": a client that had to ask a second question to render the
    badge would either ask it per row or not render it.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None = None
    popularity: float | None = None
    owned: bool = False
    score: float = 0.0


class SimilarTitle(DomainModel):
    """One neighbour of a seed title, read out of the precomputed table.

    `score` is `SimilarityService`'s blend at the instant the batch ran, not a
    live computation -- which is the whole point of `title_neighbors` (PRD 05:
    "item vectors are static, so this is a cheap batch artifact that makes
    'more like this' instant and engine-independent").
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None = None
    score: float = 0.0


__all__ = ["SearchResult", "SimilarTitle"]
