"""`GET /titles/{id}` -- PRD 03's read-through, at the boundary.

**This route cannot fail because a source is down**, and that is structural
rather than defensive: `TitleReadService` holds no `SourceAdapter`, so there
is no call to catch. PRD 08: "a degraded subsystem narrows functionality; it
never fails a request local state can answer." What a degraded source does
change is the *width* of the answer -- a copy the nightly sweep retracted is
rendered with `available: false` rather than dropped -- and never the status
code. That is why M5 shipped no RFC 9457 envelope from here: PRD 07's worked
example of one is `503 source_unavailable`, and this route has no 503 to give
a `code` to. M9 lands the envelope's *shape* anyway, driven by the surface
that already exists -- so the 404 below is a problem document while the 503
that would have forced it still does not exist on this route.

**It is a `GET` that writes**, once and deliberately: opening an unenriched
title enqueues its `enrich` job at `JobPriority.DEMAND` (PRD 03's demand
promotion). Idempotent, and a second open writes zero rows -- the enqueue
statement's `WHERE jobs.priority < excluded.priority` sees nothing left to
promote. `get_session` commits it as it commits any other request, which is
what makes the write durable rather than a flush that the response outlives.

**Twice, since M9's F3**, on the same argument and the same commit: a request
carrying `?search_id=` also attributes PRD 10's *click* to the
`search_queries` row that search wrote. It is idempotent for a second
reason -- `clicked_title_id` is first-write-wins, so re-opening a result
cannot rewrite what the search led to. `api/caching.py`'s first adoption
condition names both writes now: a conditional GET short-circuited ahead of
this handler would silently stop the promotion *and* lose the click, for
exactly the clients that already hold the title and would send
`If-None-Match` on every request.
"""

import uuid

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
from usher.api.dto.problem import ProblemCode
from usher.api.dto.similar import SimilarResponse
from usher.api.dto.title import TitleResponse
from usher.api.errors import ProblemException

router = APIRouter(tags=["titles"])


@router.get("/titles/{title_id}", response_model=TitleResponse, response_model_exclude_unset=True)
async def get_title(
    title_id: uuid.UUID,
    titles: TitleReadServiceDep,
    user_id: DefaultUserIdDep,
    queries: SearchQueryRepositoryDep,
    search_id: SearchIdDep,
) -> TitleResponse:
    """One title, everything local about it, a promotion if it needs one, and
    the click attributed to the search it came from if the client says so.

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
    resource is served.
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
    # **After the 404 and not before it.** A click on a title this deployment
    # does not have is a click on nothing, and writing one would put an id in
    # `clicked_title_id` that `fk_search_queries_clicked_title_id_titles`
    # refuses -- turning a plain 404 into a 500. The order is what makes that
    # unreachable rather than caught.
    #
    # `played=False`: this writer reports a click and nothing else. One
    # writer setting both columns is what would make `clicked_title_id` mean
    # "the last thing this household did" rather than "which result it
    # opened", and `POST /titles/{id}/play` is the other writer.
    await record_search_outcome(
        queries, search_id, user_id=user_id, clicked_title_id=title_id, played=False
    )
    return TitleResponse.of(detail)


@router.get("/titles/{title_id}/similar", response_model=SimilarResponse)
async def get_similar_titles(
    title_id: uuid.UUID, titles: TitleRepositoryDep, similarity: SimilarityServiceDep
) -> SimilarResponse:
    """M6's precomputed neighbours (`SimilarityService.neighbors_of`), plus
    both of `title_neighbors`' staleness signals -- see `SimilarResponse` for
    what each one answers and what neither can.

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
