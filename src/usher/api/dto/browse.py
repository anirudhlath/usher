"""`GET /browse`'s wire shape."""

import uuid
from enum import StrEnum

from pydantic import BaseModel

from usher.api.dto.page import Page
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.repository.title import BrowseFacets


class FacetsOmitted(StrEnum):
    """Why a response carries no counts, when it carries none.

    Two members and not one, because the two have different fixes and a client
    (or an operator reading a screenshot) has to be able to tell them apart:
    `not_requested` is answered by adding `facets=true`, `unpredicated` by
    adding a filter. Collapsing them into a single boolean would be the same
    mistake as answering an empty map -- one fact standing in for two.
    """

    NOT_REQUESTED = "not_requested"
    UNPREDICATED = "unpredicated"


class BrowseFacetsResponse(BaseModel):
    """What else this client could have asked for, counted -- or that nobody counted.

    `computed` is always present and is the field a client branches on.
    `reason` is present exactly when `computed` is false; `genres` and `years`
    exactly when it is true. The route serialises with
    `response_model_exclude_unset=True`, so "not set" really is "not on the wire".
    """

    computed: bool
    reason: FacetsOmitted = FacetsOmitted.NOT_REQUESTED
    genres: dict[str, int] = {}
    years: dict[int, int] = {}

    @classmethod
    def omitted(cls, reason: FacetsOmitted) -> "BrowseFacetsResponse":
        """No counts, and the reason there are none."""
        return cls(computed=False, reason=reason)

    @classmethod
    def of(cls, facets: BrowseFacets) -> "BrowseFacetsResponse":
        """The counts, with the maps set even when they are empty.

        An empty `genres` here means *"nothing in the filtered population
        carries a genre"*, which is a real answer -- and it is the one answer
        `omitted()` must never be mistaken for.
        """
        return cls(computed=True, genres=dict(facets.genres), years=dict(facets.years))


class BrowseItemResponse(BaseModel):
    """One row of the browse grid.

    The four sort keys are all on the wire (`name` via the row's own name,
    plus `year`, `popularity`, `vote_count`) because a client that can order by
    a value and cannot see it has to explain a sort it cannot show. `sort_name`
    is **not**: it is a catalog-ordering column, not a label, and rendering it
    would put "Matrix, The" on a card.

    `popularity` is nullable and stays nullable, for `SearchResultResponse`'s
    reason: it is `null` for every title TMDb's daily export has never described,
    most of the catalog, and `popularity or 0.0` would render "nobody has rated
    this" identically to "rated, and unpopular".

    **No artwork key**, deliberately: C6's `artwork` is one `images.id` chosen
    against a row's `display_hint`, read in one batched call by
    `services/rows/base.py`, and browse has no such read. Adding one belongs in
    the task that adds the port call, not in a DTO answering `null` for every row.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    popularity: float | None
    vote_count: int | None

    @classmethod
    def of(cls, title: Title) -> "BrowseItemResponse":
        return cls(
            title_id=title.id,
            kind=title.kind,
            name=title.name,
            year=title.year,
            popularity=title.tmdb_popularity,
            vote_count=title.tmdb_vote_count,
        )


class BrowseResponse(Page[BrowseItemResponse]):
    """A3's page envelope plus the facet block.

    A subclass rather than a third model carrying its own `items` and
    `next_cursor`: two spellings of the page envelope is how they stop
    agreeing, and `paginate` already builds one.
    """

    facets: BrowseFacetsResponse


__all__ = [
    "BrowseFacetsResponse",
    "BrowseItemResponse",
    "BrowseResponse",
    "FacetsOmitted",
]
