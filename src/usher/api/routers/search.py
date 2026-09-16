"""`GET /search` and `GET /search/suggest`.

PRD 07's `### Screens`, over the retrieval M6 finished.
"""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, status

from usher.api.deps import (
    DefaultUserIdDep,
    HouseholdDep,
    SearchServiceDep,
    VisibilityServiceDep,
)
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.search import SearchResponse, SuggestResponse
from usher.api.errors import ProblemException
from usher.ports.search import SearchMode, SuggestTier
from usher.services.search import SemanticSearchUnavailable

router = APIRouter(tags=["search"])

#: The shortest `q`, in characters after stripping, that this route will run
#: **tier 1** for.
_MIN_PREFIX_CHARS: Final = 4

#: Per tier, so the response can report the rule that actually applied.
_MIN_CHARS_FOR_TIER: Final[dict[SuggestTier, int]] = {
    SuggestTier.PREFIX: _MIN_PREFIX_CHARS,
    SuggestTier.FUZZY: 1,
}

#: What `?mode=semantic` answers on a deployment with no embedding model, and
#: it names the remedy because the remedy is the whole content of the failure.
#: Deliberately no setting name and no host: a client reads this, and
#: `USHER_EMBEDDING_ENABLED` is an operator's fact. `usher search` prints the
#: setting; this does not.
_NO_EMBEDDER_DETAIL: Final = (
    "This deployment cannot serve mode=semantic: it has no embedding model. "
    "Ask for mode=fused, which serves the full-text lane and reports the "
    "narrowing, or mode=full_text."
)

#: Declared so `/openapi.json` describes the failure with the shape it really
#: has, exactly as `api/routers/playback.py` declares its three.
_SEARCH_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    422: {
        "model": ProblemResponse,
        "description": (
            "`mode=semantic` was asked of a deployment with no embedding model. "
            "The catalog is unchanged and `mode=fused` or `mode=full_text` will answer."
        ),
    },
}


#: Suggest's own, and the two are not the same set: a `q` below its tier's
#: minimum is answered `200` with an empty list and `min_query_length`, not
#: refused, so the only failure here is a parameter FastAPI will not parse --
#: an unknown `?tier=`, a `?limit=` outside its bounds.
_SUGGEST_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}


@router.get(
    "/search",
    response_model=SearchResponse,
    responses=_SEARCH_FAILURES,
    summary="Ranked results across the catalog and the library",
)
async def search(
    search_service: SearchServiceDep,
    visibility: VisibilityServiceDep,
    # **The household, and it is a dependency rather than a query parameter.** PRD 05
    # keeps `SearchFilters` a closed vocabulary with no user field, and the reason is
    # exactly this route: anything on the query string is something a caller chooses,
    # and "whose watch history ranks this" is not a client's to choose.
    user_id: DefaultUserIdDep,
    q: Annotated[
        str,
        Query(
            description=(
                "The query as typed. Blank or whitespace-only answers 200 with no results."
            )
        ),
    ],
    mode: Annotated[
        SearchMode,
        Query(
            description=(
                "Which lanes run. `fused` narrows to `full_text` on a deployment with no "
                "embedding model and says so through `requested_mode`; `semantic` refuses."
            )
        ),
    ] = SearchMode.FULL_TEXT,
    limit: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "Ceiling on results. Clamped by `USHER_SEARCH_RESULT_LIMIT`, which is why "
                "this declares no maximum of its own."
            ),
        ),
    ] = 20,
) -> SearchResponse:
    """Retrieve, rank, and report what actually ran."""
    try:
        answer = await search_service.search(q, mode=mode, limit=limit, user_id=user_id)
    except SemanticSearchUnavailable as exc:
        # Not narrowed to full text here either, and the service is right to
        # refuse rather than answer: the caller asked the one question
        # full-text cannot answer and would otherwise get a plausible answer to
        # a different one. `usher search` makes the same call with a
        # `SystemExit`.
        raise ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=_NO_EMBEDDER_DETAIL,
        ) from exc
    # **A viewer typed this and got these rows back**, which is the strongest intent
    # signal in the API (issue #73).
    await visibility.seen_ids([result.title_id for result in answer.results])
    # `q` rather than `answer`-derived: the service is handed the typed string
    # and hands back what it ran, so echoing the parameter is the one spelling
    # that cannot accidentally echo the rewrite.
    return SearchResponse.of(q, answer)


@router.get(
    "/search/suggest",
    response_model=SuggestResponse,
    responses=_SUGGEST_FAILURES,
    summary="Type-ahead candidates, from the prefix tier or the fuzzy one",
)
async def suggest(
    search_service: SearchServiceDep,
    visibility: VisibilityServiceDep,
    # **The household read, not the household.** This route has no blend, so
    # the only thing here that needs an id is the `search_queries` row -- and
    # it writes one on neither the short-`q` arm nor a deployment with the
    # writer off. A `DefaultUserIdDep` resolves before the handler runs, which
    # would be a `users` SELECT per keystroke for an id nothing reads.
    household: HouseholdDep,
    q: Annotated[
        str,
        Query(
            description=(
                "The prefix as typed. Blank, whitespace-only, or shorter than the answering "
                "tier's `min_query_length` answers 200 with no results."
            )
        ),
    ],
    tier: Annotated[
        SuggestTier,
        Query(
            description=(
                "`prefix` is the btree probe that answers every keystroke and has no "
                "typo tolerance; `fuzzy` is the trigram path that has it, at a cost "
                "meant to be debounced behind the first. Neither is a fallback for the "
                "other."
            )
        ),
    ] = SuggestTier.PREFIX,
    limit: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "Ceiling on candidates. Clamped by `USHER_SEARCH_RESULT_LIMIT`, which is why "
                "this declares no maximum of its own."
            ),
        ),
    ] = 10,
) -> SuggestResponse:
    """Answer one tier, and say which one."""
    # `surface` says which box asked and `tier` says which index ran, so a
    # keystroke and a search do not collapse into one vocabulary -- and every
    # mode-split panel owes a `WHERE surface = 'search'`.

    # The short-`q` arm below writes nothing, and that is this route's decision
    # rather than the service's: it returns before `SearchService.suggest` is
    # called, so there is no answered query to record. A writer above this line
    # would be a row per keystroke a client never meant to send.
    minimum = _MIN_CHARS_FOR_TIER[tier]
    if len(q.strip()) < minimum:
        return SuggestResponse.of(q, tier=tier, min_query_length=minimum)
    # Bound once rather than inlined into the DTO, because the promotion below
    # has to see the same rows the response carries.
    offered = await search_service.suggest(
        q,
        limit=limit,
        tier=tier,
        # Asked of the service rather than branched on here: the switch is read
        # once, in `composition.build_search_service`, so a household resolved
        # by this route and a row written by that one cannot disagree about
        # whether this deployment records keystrokes.
        user_id=await household() if search_service.records_suggestions else None,
    )
    # **This route fires per keystroke**, which makes it the highest-volume surface on
    # the demand lane (issue #73).
    await visibility.seen_ids([result.title_id for result in offered])
    return SuggestResponse.of(
        q,
        tier=tier,
        min_query_length=minimum,
        # `tier=tier` and not a tier the service chose: the echo has to be the parameter
        # that selected the index, or a response could report a tier that did not run.
        results=offered,
    )
