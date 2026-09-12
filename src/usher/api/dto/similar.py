"""`GET /titles/{id}/similar` -- PRD 05's precomputed neighbours, over
`SimilarityService` (`services/similar.py`).
"""

import uuid
from collections.abc import Sequence
from typing import Self

from pydantic import AwareDatetime, BaseModel

from usher.domain.enums import TitleKind
from usher.domain.search import SimilarTitle

__all__ = ["SimilarResponse", "SimilarTitleResponse"]


class SimilarTitleResponse(BaseModel):
    """One neighbour, in the **stored order** -- `SimilarityService.
    neighbors_of` already reads `title_neighbors` back by its own stamped
    `rank`, best first, ties broken by id (`ports/repository/search.py`'s
    `TitleNeighborRepository.list_for`), and this DTO never re-sorts on
    `score`. Reproducing the order from the score works only up to float
    ties, and a tie broken differently on two reads would show a client two
    different "most similar" titles for the same catalog.
    """

    id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    score: float

    @classmethod
    def of(cls, neighbor: SimilarTitle) -> Self:
        return cls(
            id=neighbor.title_id,
            kind=neighbor.kind,
            name=neighbor.name,
            year=neighbor.year,
            score=neighbor.score,
        )


class SimilarResponse(BaseModel):
    """Neighbours, plus both of `title_neighbors`' staleness signals -- reported rather
    than implied, because a client that could not see either one would be shown
    yesterday's neighbours (or none at all) with no way to tell that from "this title
    genuinely has nothing like it".
    """

    neighbors: list[SimilarTitleResponse]
    computed_at: AwareDatetime | None
    stale: bool

    @classmethod
    def of(
        cls,
        neighbors: Sequence[SimilarTitle],
        *,
        computed_at: AwareDatetime | None,
        stale: bool,
    ) -> Self:
        return cls(
            neighbors=[SimilarTitleResponse.of(neighbor) for neighbor in neighbors],
            computed_at=computed_at,
            stale=stale,
        )
