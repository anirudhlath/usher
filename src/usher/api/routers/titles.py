"""`GET /titles/{id}` -- PRD 03's read-through, at the boundary."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.analytics import record_search_outcome
from usher.api.deps import (
    DefaultUserIdDep,
    SearchIdDep,
    SearchQueryRepositoryDep,
    SimilarShelfServiceDep,
    TitleReadServiceDep,
)
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.similar import SimilarResponse
from usher.api.dto.title import TitleResponse
from usher.api.errors import ProblemException

router = APIRouter(tags=["titles"])

#: What `/openapi.json` says these routes answer when they fail. The `422` is
#: declared rather than left to FastAPI, whose automatic one names
#: `HTTPValidationError` while `api/errors.py` answers an RFC 9457 document
#: carrying the same error list under `errors`.
#: `tests/unit/test_api_openapi.py` holds both halves.
_TITLE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such title."},
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}


@router.get(
    "/titles/{title_id}",
    response_model=TitleResponse,
    response_model_exclude_unset=True,
    responses=_TITLE_FAILURES,
)
async def get_title(
    title_id: uuid.UUID,
    titles: TitleReadServiceDep,
    user_id: DefaultUserIdDep,
    queries: SearchQueryRepositoryDep,
    search_id: SearchIdDep,
) -> TitleResponse:
    """One title: everything local about it, plus a promotion if it needs one.

    **`response_model_exclude_unset=True` makes an empty `cast` or `crew` an
    absent key rather than `[]`** -- `TitleResponse.of` declines to *set* either
    when it has no members, and sets every other field unconditionally.

    **`?search_id=` is PRD 10's click.** `GET /search` hands back the id of the
    `search_queries` row it wrote; opening a result with that id attached is the
    only moment anything knows *which* result the household opened, so it fills
    `clicked_title_id`. It changes no status code, no field and no header.
    Omitting it is always legal, and a value that is unknown or not a UUID at all
    is ignored rather than refused.
    """
    detail = await titles.detail(title_id, user_id=user_id)
    if detail is None:
        # PRD 07's envelope, in the one line adopting it costs. `not_found` is
        # generic on purpose: the vocabulary is settled once for every route
        # rather than five times.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    # **After the 404 and not before it.** A click on a title this deployment does not
    # have is a click on nothing, and writing one would put an id in `clicked_title_id`
    # that `fk_search_queries_clicked_title_id_titles` refuses -- turning a plain 404
    # into a 500.
    await record_search_outcome(
        queries, search_id, user_id=user_id, clicked_title_id=title_id, played=False
    )
    return TitleResponse.of(detail)


@router.get("/titles/{title_id}/similar", response_model=SimilarResponse, responses=_TITLE_FAILURES)
async def get_similar_titles(title_id: uuid.UUID, shelf: SimilarShelfServiceDep) -> SimilarResponse:
    """The title's stored "Because you watched" row, else its neighbours, as home cards.

    Reads `seed_rows` and `title_neighbors`, and for an enriched title whose record is due
    may queue one `seed_row` job. A title with nothing similar is `200` with no cards; only
    an unknown `title_id` is `404`.
    """
    found = await shelf.read(title_id)
    if found is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    return SimilarResponse.of(found)
