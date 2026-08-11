"""`GET /collections/{id}` -- PRD 07's franchise page, at the boundary.

One read and a hydration. `CollectionRepository.get` answers the franchise and
the household's coverage of it in one statement -- two lists, so the two counts
are `len()` and cannot disagree -- and `TitleRepository.list_by_ids` turns the
member ids into cards. The ownership predicate stays in SQL, where B6 settled
it: an **available**, **title-level** media item.

**`get` is not `list_owned` with a filter, and the difference is `min_owned`.**
`list_owned`'s floor of 2 is a statement about what belongs on a *screen* -- a
franchise you own one of is a single film with a subtitle. This route answers a
franchise the client asked for by id, so there is no floor at all: "you own 1 of
4" is the honest answer, and it is the one a household that has barely started a
series most wants to be told.

**A franchise the household owns none of is a 200**, and only a franchise the
catalog does not hold is a 404. Collapsing the two would make "you own 0 of 7"
unreachable, which is a real answer for a client that followed a link from a
film it does own.

**No cursor, stated as a bound rather than assumed.** TMDb franchises are
single-digit to low-double-digit members, and the hydration is one statement
over all of them. The day one is unbounded it is the same opaque codec
`GET /browse` uses over the keyset shape it already has.

**No user.** Ownership is a property of the household's sources -- `MediaItem`
has no user and never has -- which is the argument `list_owned` already makes
about carrying no `user_id`.
"""

import uuid

from fastapi import APIRouter, status

from usher.api.deps import CollectionRepositoryDep, TitleRepositoryDep
from usher.api.dto.collection import CollectionResponse
from usher.api.dto.problem import ProblemCode
from usher.api.errors import ProblemException

router = APIRouter(tags=["collections"])


@router.get("/collections/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: uuid.UUID,
    collections: CollectionRepositoryDep,
    titles: TitleRepositoryDep,
) -> CollectionResponse:
    """A franchise, its members in release order, and how much of it the
    household owns.

    `owned_count` and `total_count` are the lengths of the rendered list and of
    its owned subset, so a client that counts the cards gets the same numbers.
    Every member is present whether or not the household has a copy: a list
    filtered to the owned subset reads "2 of 2", which is a completeness signal
    that always reads complete.

    `owned` means an available, title-level copy. A collection holds only
    movies -- `belongs_to_collection` is a field of TMDb's `/movie/{id}` with no
    `/tv/{id}` counterpart -- so a series carrying a collection id is a defect
    and is not a member here.

    A **404** for a franchise the catalog does not hold; a **200** with
    `owned_count: 0` for one it holds and the household owns none of.
    """
    collection = await collections.get(collection_id)
    if collection is None:
        # V1's vocabulary, generic on purpose: RFC 9457's `instance` already
        # carries `/collections/{id}`, so a `collection_not_found` member would
        # be a second spelling of what the document says. ADR-0030.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="collection not found",
        )
    # One statement for every member rather than a `get()` apiece, and skipped
    # entirely for a franchise with no members: `IN ()` is a round trip to learn
    # nothing.
    hydrated = await titles.list_by_ids(list(collection.title_ids)) if collection.title_ids else []
    return CollectionResponse.of(collection, hydrated)
