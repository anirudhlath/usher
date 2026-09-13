"""`GET /titles/{id}` -- PRD 03's read-through, at the boundary."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.analytics import record_search_outcome
from usher.api.deps import (
    DefaultUserIdDep,
    SearchIdDep,
    SearchQueryRepositoryDep,
    SimilarityServiceDep,
    TitleReadServiceDep,
    TitleRepositoryDep,
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
    """One title.

    everything local about it, a promotion if it needs one, and the click attributed to
    the search it came from if the client says so.

    **`response_model_exclude_unset=True` is what makes an empty `cast` or
    `crew` an absent key rather than `[]`** -- `TitleResponse.of` declines to
    *set* either when it has no members, and every other field it sets
    unconditionally, so nothing else moves. The reasoning, the two spellings
    rejected and the guard that keeps `of` honest are all in
    `api/dto/title.py`; this flag is the half that cannot live there.

    **`?search_id=` is PRD 10's click, and it is the reason this route now
    writes twice.** `GET /search` hands the id of the `search_queries` row it
    wrote; opening a result with that id attached is the only moment anything
    knows *which* result the household opened, so it fills
    `clicked_title_id`. It rides the same commit the demand promotion does
    and changes nothing else: no status code, no field, no header. Omitting
    it is always legal, and a value that is unknown or not a UUID at all is
    ignored rather than refused -- analytics may not decide whether a
    """
    detail = await titles.detail(title_id, user_id=user_id)
    if detail is None:
        # PRD 07's envelope, in the one line adopting it costs. `not_found`
        # is generic on purpose *and provisionally*: whether this becomes
        # `title_not_found` is ADR-0030's call, not this router's, and it is
        # settled once for every route rather than five times.
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
async def get_similar_titles(
    title_id: uuid.UUID, titles: TitleRepositoryDep, similarity: SimilarityServiceDep
) -> SimilarResponse:
    """M6's precomputed neighbours (`SimilarityService.neighbors_of`).

    plus both of `title_neighbors`' staleness signals -- see `SimilarResponse` for what
    each one answers and what neither can.

    A title with no stored neighbours is `200` with an empty list -- that is
    a fact about the title, not a failure -- and only an unknown `title_id`
    is `404`, the same existence read `POST /titles/{id}/play` makes
    (`api/routers/playback.py`) rather than a second definition of "not
    found".

    **No `Embedder`, and no `SourceAdapter`.** Every signal this route reads
    is precomputed: `SimilarityService` takes two repositories and a title
    repository, never a model or a source, so there is no live computation
    here to fail and no source outage that could turn into one.
    """
    if await titles.get(title_id) is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    neighbors = await similarity.neighbors_of(title_id)
    computed_at = await similarity.computed_at()
    stale = await similarity.stale_neighbors(title_id=title_id) > 0
    return SimilarResponse.of(neighbors, computed_at=computed_at, stale=stale)
