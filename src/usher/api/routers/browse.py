"""`GET /browse` -- PRD 07's faceted paging screen, over B6's two reads.

**A screen with nothing on it is a fact about the catalog and the filters, not
a missing resource.** Every empty answer here is a `200` with an empty list and
a `null` cursor. The only 404 this module can produce is FastAPI's own, for a
path that is not this one.

**The facet block is `?facets=true` plus a predicate, and the measurement is
why.** B7's bar -- registered at `/var/tmp/m9-B7/BAR.md` before the first probe
-- put unfiltered facet counts at p95 <= 200 ms over 1.27M titles. Measured:
**330.81 ms**. The plan's named consequence was to serve facets only for a
predicated browse behind an explicit key, and the same run refuted that as a
remedy: a genre-predicated request costs **324.43 ms**, because
`browse_facets` computes each facet over the population *minus its own
predicate*, so a genre-only request counts genres over the whole catalog by
construction. `api/dto/browse.py` carries the full table. What ships is the
plan's rule with the measurement's gate in front of it.

**A cursor is minted for one `(sort, filters)` and refused for any other.** The
refusal is A3's codec at this router, never the port: `CursorSpec.filters`
carries the genre, the year and the owned flag, so a cursor minted under
`sort=year&genre=horror` and replayed against `sort=name` is a
`400 invalid_cursor` rather than a plausible, complete, wrong page. `limit` is
deliberately **not** in the digest -- a client changing its page size mid-walk
has not changed the population or its order, and refusing that would be a
refusal with nothing behind it.
"""

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Query

from usher.api.cursor import CursorSpec, CursorType, decode_cursor, over_fetch, paginate
from usher.api.deps import TitleRepositoryDep
from usher.api.dto.browse import (
    BrowseFacetsResponse,
    BrowseItemResponse,
    BrowseResponse,
    FacetsOmitted,
)
from usher.ports.repository.title import BrowseCursorPosition, BrowseSort

router = APIRouter(tags=["browse"])

#: A browse grid is a multiple of six on every breakpoint a client renders, so
#: 24 fills one and 48 fills two. The ceiling is what stops a client asking for
#: a 1.27M-row page by widening a query parameter, and both are module
#: constants rather than `Settings` fields: this group adds no configuration,
#: and a page size an operator can tune is a contract clients cannot rely on.
DEFAULT_LIMIT = 24
MAX_LIMIT = 100

#: Each sort's keyset component types, beside `BrowseSort` rather than derived
#: from it, because the wire type of a sort key is a fact about the **codec**
#: and `_ORDERS` is a fact about the *table*. `CursorSpec` refuses a keyset
#: that does not end in the UUIDv7 primary key, so the trailing `UUID` is not
#: decoration -- it is the total order (ADR-0003, ADR-0034).
#:
#: `test_every_sort_has_a_cursor_type` is what keeps this exhaustive: a fifth
#: `BrowseSort` member with no entry here would raise `KeyError` inside a
#: route, which is a 500 for a value the enum says is legal.
_KEYSET_TYPES: dict[BrowseSort, tuple[CursorType, ...]] = {
    BrowseSort.NAME: (CursorType.STR, CursorType.UUID),
    BrowseSort.YEAR: (CursorType.INT, CursorType.UUID),
    BrowseSort.POPULARITY: (CursorType.FLOAT, CursorType.UUID),
    BrowseSort.VOTE_COUNT: (CursorType.INT, CursorType.UUID),
}


def _keyset(
    sort: BrowseSort, *, genre: str | None, year: int | None, owned: bool | None
) -> CursorSpec:
    """This query's cursor identity: the sort, and every filter that narrows
    the population.

    The filters ride in `filters` and not in the keyset, which is what makes a
    cursor minted over `genre=horror` and replayed against `genre=comedy` a
    `400 invalid_cursor` instead of a page of comedies resumed from a horror
    film's position. ADR-0034: the digest is coherence, not security -- it is
    computed over values the client itself sent, and the client is the only
    party that ever holds the cursor.

    Only the filters that are *set* are rendered, so an absent `genre` and
    `genre=` unset are one query rather than two. `limit` is absent on purpose:
    it changes how much of the population a page shows and not which
    population, in what order.
    """
    filters = {
        name: str(value)
        for name, value in (("genre", genre), ("year", year), ("owned", owned))
        if value is not None
    }
    return CursorSpec(sort=sort.value, types=_KEYSET_TYPES[sort], filters=filters)


def _after(cursor: str | None, *, spec: CursorSpec) -> BrowseCursorPosition | None:
    """The wire cursor as the typed position the port takes.

    ADR-0034's first decision, spelled: the base64 stops here. A port that
    accepted a cursor would have to decode one, which means knowing the sort
    vocabulary of the layer above it.

    `cast` rather than a runtime check, and the codec is the reason: every
    component is type-checked against `spec.types` inside `decode_cursor`
    before it returns -- or the call already raised `400 invalid_cursor`. A
    defensive `isinstance` here would be a branch no input can reach, which is
    the kind of guard that reads as coverage and is not. The one arm that
    *does* need saying is `None`: a NULL sort key is a **position**, not a
    missing value, because browse orders NULLs last and a page boundary can
    land inside the unkeyed group.
    """
    if cursor is None:
        return None
    key, identifier = decode_cursor(cursor, spec=spec)
    return BrowseCursorPosition(
        key=cast(str | int | float | None, key), id=cast(uuid.UUID, identifier)
    )


@router.get(
    "/browse",
    response_model=BrowseResponse,
    response_model_exclude_unset=True,
    summary="One keyset page of the catalog, filtered and sorted",
)
async def browse_catalog(
    titles: TitleRepositoryDep,
    sort: Annotated[BrowseSort, Query()] = BrowseSort.NAME,
    genre: Annotated[str | None, Query()] = None,
    year: Annotated[int | None, Query(ge=0)] = None,
    owned: Annotated[bool | None, Query()] = None,
    facets: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> BrowseResponse:
    """One page of the catalog, keyset-paged, with the facet counts the
    measurement allows.

    **An empty page is `200`, an empty list and a `null` cursor.** `/browse` is
    a screen, and a screen with nothing on it is a statement about the catalog
    and the filters a client typed -- there is no resource missing. The last
    page's cursor is `null` rather than a cursor that yields an empty page:
    `over_fetch(limit)` asks for one row more than it serves, and that row is
    the whole difference between "this page is full" and "there is more". A
    cursor that never nulls is an infinite client loop that every finite test
    passes.

    **`owned` is three-valued** -- absent, `true`, `false` -- because a
    two-valued flag makes "unset" and "the user asked for unowned" the same
    request. The port settles what the word means: an available, title-level
    copy.

    One statement for the page, and two more only if facets were both asked for
    and affordable.
    """
    spec = _keyset(sort, genre=genre, year=year, owned=owned)
    fetched = await titles.browse(
        sort=sort,
        genre=genre,
        year=year,
        owned=owned,
        after=_after(cursor, spec=spec),
        limit=over_fetch(limit),
    )
    page = paginate(
        fetched,
        limit=limit,
        spec=spec,
        # Reads the **row**, not the DTO: `sort_name` is the column
        # `BrowseSort.NAME` orders by and `BrowseItemResponse` deliberately
        # does not carry it, which is `paginate`'s own reason for taking two
        # callables. `position_of` reads the key from `_ORDERS`, so the route
        # is not a second definition of what `year` means.
        keys=lambda one: (BrowseSort.position_of(one, sort=sort).key, one.id),
        item=BrowseItemResponse.of,
    )
    return BrowseResponse(
        items=page.items,
        next_cursor=page.next_cursor,
        facets=await _facets(titles, asked=facets, genre=genre, year=year, owned=owned),
    )


async def _facets(
    titles: TitleRepositoryDep,
    *,
    asked: bool,
    genre: str | None,
    year: int | None,
    owned: bool | None,
) -> BrowseFacetsResponse:
    """The counts, or an explicit statement that nobody counted them.

    Two gates, in the order a client can act on. `not_requested` first, because
    a client that did not ask should be told that rather than told its filters
    were wrong -- and because that is the cheap answer on the default request,
    which is the one every browse screen makes.
    """
    if not asked:
        return BrowseFacetsResponse.omitted(FacetsOmitted.NOT_REQUESTED)
    if genre is None and year is None and owned is None:
        # 330.81 ms p95 at 1,272,367 titles, against a 200 ms bar. The refusal
        # is the plan's named consequence of that number and it is the reason
        # this route has a `computed` field at all.
        return BrowseFacetsResponse.omitted(FacetsOmitted.UNPREDICATED)
    return BrowseFacetsResponse.of(await titles.browse_facets(genre=genre, year=year, owned=owned))
