"""`GET /search` — PRD 07's `### Screens`, over the retrieval M6 finished.

**Why a route only now.** M6 built `SearchService`, `PostgresSearchIndex`, RRF
fusion and the ranking blend and deliberately added **no HTTP route**,
delivering the whole capability through `usher search` (PRD 09's M6 boundary
call 1) — the same call M2 made for `bootstrap` and M4 for the ingest pipeline.
So this module adds a router over finished wiring and changes no ranking term,
no SQL and no setting.

**Two shape decisions were recorded before this route existed and both land
here.**

1. **`SearchMode` reaches the wire three-valued** — `full_text | semantic |
   fused`, the port's own enum — rather than as PRD 07's sketched `semantic=`
   boolean, because a bool cannot express fusion at all and fusion is the
   design (ADR-0002). **`?semantic=` is not accepted**: shipping both would put
   two vocabularies on one field, and `?semantic=true&mode=full_text` has no
   answer anybody would agree on. PRD 07's Screens row is corrected in the same
   commit rather than left to disagree with the code.
2. **`expanded_query` reaches the response body.** M8 put an LLM rewrite in
   front of the semantic embed under the rule that it is *reported, never
   silently substituted*, and `usher search` prints it above the results. A
   route that dropped the field would make an expansion invisible to exactly
   the surface most people search from — the same class of defect
   `requested_mode` beside `mode` prevents one field over. `api/dto/search.py`
   carries the one-directional rule that travels with it.

**`limit` is clamped once, at the service.** `SearchService.search` does
`min(limit, self._result_limit)` against the ceiling `composition` reads out of
`Settings` (`USHER_SEARCH_RESULT_LIMIT`), so this route declares a floor and no
ceiling. A `le=` here would be the same ceiling spelled twice — two numbers that
agree until somebody moves one, and the one that moves is whichever is not
beside the `Settings` field. Nothing in this module reads it, and that absence
is asserted rather than described.

**The setting's dotted spelling is deliberately absent from this file, prose
included.** `tests/unit/test_config.py::test_every_setting_is_read_by_something`
proves no `Settings` field is a knob with no effect by joining every module
under `src/usher/` and asking whether `.<name>` appears anywhere — so a
docstring writing the attribute access would keep that field's check green on
behalf of a reader that does not exist here, and would go on doing so if
`composition.py`'s real read were ever deleted. Recorded in
`.claude/rules/testing-discipline.md` as *prose that answers a scan*, which is
the failure mode that entry was added for.

**A blank or whitespace-only `q` is a `200` with no results**, matching
`SearchService`'s own `if not query.strip()` guard, and it buys no completion:
the guard returns before the embed an expansion would sit in front of. Not a
422 — a search box sends one between keystrokes, and rejecting it would put an
error on the wire for every viewer who selected their query and typed over it.

**This route holds no `SourceAdapter` and no embedding model**, so it has no
503 to give a code to and cannot 500 on a push-only deployment. It is
`GET /home`'s property, one route over, and it is what makes `?mode=semantic`
the only failure below.
"""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, status

from usher.api.deps import SearchServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.search import SearchResponse
from usher.api.errors import ProblemException
from usher.ports.search import SearchMode
from usher.services.search import SemanticSearchUnavailable

router = APIRouter(tags=["search"])

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
#:
#: **422 `validation_failed`, and the choice is constrained rather than
#: chosen.** `SemanticSearchUnavailable`'s own docstring rules out the two
#: candidates a reader reaches for first: it is deliberately not a
#: `UsherPortError`, because nothing failed — the deployment was configured
#: without a model and said so once, at startup — so `503 source_unavailable`
#: would say *retry* about a state no retry reaches, and `source_unavailable`
#: names a media server besides. That leaves the axis this genuinely sits on:
#: the request is well formed and names a mode this server cannot process, and
#: the client's remedy is to change the request. RFC 9110's 422 is exactly
#: that, and it is what `GET /events` already answers for a `?titles=` it
#: refuses. **ADR-0030 closed the vocabulary at seven and none of the seven
#: means "this deployment lacks a capability"; nothing is minted here.** If a
#: later route needs to distinguish "your parameter is malformed" from "your
#: parameter is unserviceable *here*", that is an amendment to ADR-0030 and
#: not a decision this route may take on its own.
_SEARCH_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    422: {
        "model": ProblemResponse,
        "description": (
            "`mode=semantic` was asked of a deployment with no embedding model. "
            "The catalog is unchanged and `mode=fused` or `mode=full_text` will answer."
        ),
    },
}


@router.get(
    "/search",
    response_model=SearchResponse,
    responses=_SEARCH_FAILURES,
    summary="Ranked results across the catalog and the library",
)
async def search(
    search_service: SearchServiceDep,
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
    """Retrieve, rank, and report what actually ran.

    ⚠️ **`mode=semantic` cannot succeed on an API-only deployment**, and that
    is a property of the wiring rather than of this route: `create_app`'s
    lifespan builds an embedding model **only when `worker_enabled`** and does
    not expose it, so `api/deps.get_search_service` holds none. `mode=fused`
    narrows to full text and reports the narrowing; `mode=semantic` answers the
    422 below. Closing it is a new capability — expose the lifespan's model, or
    build a second one per API process at 65 MB and a ~4.8 s cold load — not a
    change here.

    The `try` wraps the call and nothing else. `SemanticSearchUnavailable` is
    raised before any retrieval, so there is no partial answer to discard and
    no second failure mode hiding inside the block.
    """
    try:
        answer = await search_service.search(q, mode=mode, limit=limit)
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
    # `q` rather than `answer`-derived: the service is handed the typed string
    # and hands back what it ran, so echoing the parameter is the one spelling
    # that cannot accidentally echo the rewrite.
    return SearchResponse.of(q, answer)
