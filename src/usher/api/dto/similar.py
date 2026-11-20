"""`GET /titles/{id}/similar`: a title's stored row, or its neighbours, as home cards."""

from typing import Self

from pydantic import BaseModel

from usher.api.dto.home import RowCardResponse
from usher.domain.rows import SimilarShelf, SimilarSource

__all__ = ["SimilarResponse"]


class SimilarResponse(BaseModel):
    """Cards in the order served; `building` is true while a build for the title is queued."""

    cards: list[RowCardResponse]
    source: SimilarSource
    building: bool

    @classmethod
    def of(cls, shelf: SimilarShelf) -> Self:
        return cls(
            cards=[RowCardResponse.of(card) for card in shelf.cards],
            source=shelf.source,
            building=shelf.building,
        )
