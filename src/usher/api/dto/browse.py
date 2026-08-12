"""`GET /browse`'s wire shape -- **written after the measurement, because the
measurement changed it.**

B7's bar was registered before the first probe (`/var/tmp/m9-B7/BAR.md`,
`sha256 256f28ba8102a4...`, and restated in `scripts/measure_browse.py`):
unfiltered facet counts p95 <= 200 ms at 1.27M titles. It **failed at
330.81 ms**, and the plan's named consequence -- *"facets are served only for
a predicated browse and the response says so with an explicit key rather than
an empty facet map"* -- is why `BrowseFacetsResponse` has a `computed` field at
all. An empty map and "the server did not compute these" are two different
facts about the catalog and a client cannot tell them apart.

⚠️ **The measurement also refuted the remedy the plan named, and that is why
the condition is narrower than "predicated".** A genre-predicated facet
request measured **324.43 ms** -- indistinguishable from the unfiltered 330.81
-- because `TitleRepository.browse_facets` computes each facet over the
filtered population **minus its own predicate**, so a request whose only filter
is a genre computes the genre facet over the *whole* catalog by construction.
Predicating on a genre cannot make facets affordable; it was never going to.
Only a `year` predicate moved the number, to 201.12 ms, which still fails a bar
with no tolerance, and only `genre` **and** `year` together came in under it at
194.92 ms.

So facets are **opt-in and predicated**: `?facets=true` *and* at least one of
`genre`, `year`, `owned`. The opt-in is what the measurement forced on top of
the plan's rule -- it narrows the plan's condition rather than widening it, and
it is the only thing that stops a default browse paying 331 ms for counts most
screens never render. `not_requested` and `unpredicated` are two reasons and
they are reported separately, because they have two different fixes.

**`genres` and `years` are absent when the counts were not computed, and
present-and-possibly-empty when they were.** That is `api/dto/page.py`'s stated
rule applied in both directions: *"a key is absent when its value could never
be anything else, and present-and-null when a client has to branch."* A
not-computed facet map could never be anything but empty, so it is absent; a
*computed* one is legitimately `{}` when the filter matches nothing, and that
is a fact the client must be able to read.
"""

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
    """What else this client could have asked for, counted -- or an explicit
    statement that nobody counted.

    `computed` is always present and is the field a client branches on.
    `reason` is present exactly when `computed` is false; `genres` and `years`
    exactly when it is true. The route serialises with
    `response_model_exclude_unset=True`, so "not set" really is "not on the
    wire" -- and `test_the_facet_response_carries_every_field_of_its_own_model`
    is what stops a field added here and forgotten in the two constructors
    below from silently vanishing instead of failing.
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
    recorded reason: it is `null` for every title TMDb's daily export has never
    described -- **980,523 of the 1,272,367 rows** this route was measured
    against -- and `popularity or 0.0` would render "nobody has measured this"
    identically to "measured, and unpopular" (ADR-0014).

    **No artwork key**, deliberately: C6's `artwork` is one `images.id` chosen
    against a row's `display_hint`, read in one batched call by
    `services/rows/base.py`, and browse has no such read. Adding one here is
    additive and belongs in the task that adds the port call, not in a DTO
    that would have to answer `null` for every row.
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
            popularity=title.popularity,
            vote_count=title.vote_count,
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
