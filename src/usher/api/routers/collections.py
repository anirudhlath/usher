"""`GET /collections/{id}` -- PRD 07's franchise page, at the boundary."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import CollectionRepositoryDep, TitleRepositoryDep
from usher.api.dto.collection import CollectionResponse
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException

router = APIRouter(tags=["collections"])

#: What `/openapi.json` says this route answers when it fails. The `422` is
#: declared rather than left to FastAPI, whose automatic one names
#: `HTTPValidationError` while `api/errors.py` answers an RFC 9457 document
#: carrying the same error list under `errors`.
#: `tests/unit/test_api_openapi.py` holds both halves.
_COLLECTION_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such collection."},
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    responses=_COLLECTION_FAILURES,
)
async def get_collection(
    collection_id: uuid.UUID,
    collections: CollectionRepositoryDep,
    titles: TitleRepositoryDep,
) -> CollectionResponse:
    """A franchise, its members in release order, and how much of it the household owns.

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
        # The shared vocabulary, generic on purpose: RFC 9457's `instance` already
        # carries `/collections/{id}`, so a `collection_not_found` member would be a
        # second spelling of what the document says.
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
