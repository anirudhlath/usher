"""`GET /search` and `GET /search/suggest` — PRD 07's `### Screens`, over the
retrieval M6 finished.

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

## `GET /search/suggest` — two tiers on one route (ADR-0031)

**The server does not debounce; the client does.** ADR-0002's typo-tolerance
gate failed and what it bought is two indexes rather than one tuned index, so
this route's whole job is to make the two *separately askable* and to say which
one answered. Nothing here holds a timer, coalesces a request, or drops one:
a debounce is a decision about a keyboard, the server has never seen one, and a
server-side one would add latency to the tier whose entire purpose is not to
have any. The gate's own conclusion is the design — *"btree prefix on every
keystroke, the trigram path debounced behind it"* — and both halves of that
sentence name the client.

**Tier 2's latency is the cost the split exists to keep off a keystroke**, and
it is unchanged by this route: the gate measured the shipped configuration at
**p50 33.6 ms / p95 211 ms / max 730 ms** against a 50 ms as-you-type budget —
over by 4x — and M9's B3 reproduced it at **p50 39.59 ms** on a different draw
of the same 2,993 cases against the same catalog. This route adds a parameter
and a DTO; it changes no statement, no floor and no cap.

⚠️ **But "tier 2's latency is what the split buys" is only true from seven
characters up, and stating it as a single p50 gets the short end exactly
backwards.** B3 measured tier 1 *per prefix length*, which is the workload a
keystroke actually is — the gate's 0.6 ms figure was over whole mutated names,
which are long and selective. p95, on the 1,271,138-title catalog with a
10,896,525-row `title_search_names` arm:

| characters typed | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| tier 1, `titles` only | 291 ms | 51 ms | 15 ms | 5 ms | 19 ms | 14 ms | 2.0 ms | 2.3 ms |
| tier 1, union | **2,707 ms** | 809 ms | 303 ms | 112 ms | 100 ms | 86 ms | 2.3 ms | 2.6 ms |

**Below seven characters tier 1 is the slower tier**, by up to two orders of
magnitude at the first keystroke — so at the short end the split's cost and its
benefit are the other way round from the sentence above, and a reader given one
p50 would conclude the opposite of what was measured. Both arms miss a 10 ms
budget below seven characters, which is why B3 declined to narrow the union: it
would have moved a 291 ms first keystroke to where a 2,707 ms one had been. The
mechanism is not the sort (a 26 kB top-N heapsort) but the `UNION`'s
de-duplication spilling 47 MB and a lossy bitmap heap recheck. ADR-0031 carries
the full argument and the alternatives.

**Hence `_MIN_PREFIX_CHARS`, which is this route's answer and the only lever a
request boundary has.** See its own comment for why four.

**No household, unlike `GET /search`.** `SearchService.suggest` takes no
`user_id` — the suggest path runs no blend, so there is no watch-state term, no
taste term and nothing for a household to change. A `DefaultUserIdDep` here
would be a `SELECT` (and, on a first run, an `INSERT`) **per keystroke** to
resolve an id nothing downstream reads.

**No completion is ever bought on this path, on either tier**, and that is
structural rather than a rule to remember: `QueryExpansionService.expand` is
called from exactly one line in `SearchService.search`, immediately in front of
the semantic embed, and `suggest` has no embed at all. So the surface a client
drives per keystroke cannot reach an LLM even with every switch on.
"""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, status

from usher.api.deps import DefaultUserIdDep, SearchServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.search import SearchResponse, SuggestResponse
from usher.api.errors import ProblemException
from usher.ports.search import SearchMode
from usher.services.search import SemanticSearchUnavailable, SuggestTier

router = APIRouter(tags=["search"])

#: The shortest `q`, in characters after stripping, that this route will run
#: **tier 1** for. Below it the answer is `200` with no results and no query
#: issued at all.
#:
#: **Four, and it is derived from B3's curve rather than chosen.** The rule is
#: *the shortest prefix at which tier 1's measured p95 is below tier 2's* —
#: because the one thing the two-tier split rests on is that tier 1 is the
#: cheap tier, and wherever that is false the split is upside down. Tier 2's
#: shipped p95 is 211 ms; tier 1's union p95 is 303 ms at three characters and
#: **112 ms at four**, so four is where tier 1 stops being slower than the tier
#: it exists to be cheaper than. It also removes the three worst probes on the
#: curve (2,707 / 809 / 303 ms), which is where essentially all of the cost is.
#:
#: **Not the 10 ms keystroke bar, deliberately.** That bar is met only from
#: seven characters up, and a minimum of seven would leave the tier that exists
#: to answer every keystroke answering nothing for most of a typed word — worse
#: for a viewer than a 112 ms box, and it would make tier 1 useless on the
#: short one-word names (`Dune`, `Alien`) that are the entire reason ADR-0002's
#: gate failed.
#:
#: **Not a `Settings` field.** The number is a property of catalog size, not of
#: an operator's preference — at the 10,000-title enriched tier the same
#: one-character probe is 489 ms and four characters is 5.5 ms — and PRD 05's
#: own note puts the decision at the request boundary. A knob here would be a
#: latency budget expressed as a character count, which is a number nobody can
#: set correctly without re-running B3.
#:
#: **Measured on the stripped string.** `len(q)` would let four spaces past a
#: bound that exists to keep a one-character probe off the database: leading
#: whitespace contributes no selectivity to `LIKE 'q%'`, so it must not count
#: toward the length that stands in for selectivity.
_MIN_PREFIX_CHARS: Final = 4

#: Per tier, so the response can report the rule that actually applied.
#:
#: **Tier 2 is bounded at one character and not at four, and the asymmetry is
#: honest rather than tidy.** Nobody has measured the trigram statement's
#: latency *per prefix length* — B3's tier-2 figures are over whole mutated
#: names — so a bound here would be a number with no measurement under it, and
#: this repository's own rule is that a refusal justified by "this cannot be
#: expensive" is one measurement away from being wrong. Tier 2's defence is the
#: client's debounce, which is the design; tier 1's is this table, because a
#: debounce cannot defend the tier that runs on every keystroke by definition.
#: One character rather than zero: a blank or whitespace-only `q` is the state
#: of every page load and of every backspace to zero, and it answers 200 with
#: no results on **both** tiers through this same rule.
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
    # **The household, and it is a dependency rather than a query parameter.**
    # PRD 05 keeps `SearchFilters` a closed vocabulary with no user field, and
    # the reason is exactly this route: anything on the query string is
    # something a caller chooses, and "whose watch history ranks this" is not a
    # client's to choose. Until PRD 01's authentication seam is filled it is
    # the singleton default user, resolved the same way `PUT /watch/...`
    # resolves it -- so the day a request carries an identity, one dependency
    # changes and this line does not.
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

    **The same `q` can answer differently for two households**, because the
    blend now carries a watch-state term. Nothing in the response says which
    household answered, and that is not the omission `requested_mode` beside
    `mode` exists to prevent: a degraded mode is a deployment state a client
    cannot otherwise observe, whereas every request to this route carries a
    household by construction, so there is no unpersonalised answer for a field
    to distinguish. `SearchService`'s own docstring records what changes when
    authentication makes one reachable.
    """
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
                "`prefix` is the btree probe that answers every keystroke and has no typo "
                "tolerance (1.9% measured); `fuzzy` is the trigram path that has it, at "
                "p50 33.6 ms, and is meant to be debounced behind the first. Neither is a "
                "fallback for the other."
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
    """Answer one tier, and say which one.

    **The short-`q` arm returns before the service, not inside it**, and that
    placement is the whole point: what a one-character prefix costs is 2,707 ms
    *of database work*, so a bound applied after the port call would save
    nothing at all. It is also why this lives here rather than in
    `SearchService` — `usher suggest` is a command typed once, not a keystroke,
    and refusing it a three-character prefix would take a capability away from
    the one caller that can afford it (ADR-0031).

    **`min_query_length` is reported on every response, not only the refused
    ones.** A client that can read the rule can apply it and never send the
    request, which is the only place this cost can actually be removed rather
    than moved; and on a full answer the same field is what says the box is
    complete rather than truncated by a bound.

    **No `try`, because this route has no failure to catch.**
    `SemanticSearchUnavailable` is raised in front of an embed and `suggest`
    has none — no model, no lane to narrow, no capability an operator may not
    have installed. Both tiers are btree/GIN reads over tables `m09a` creates
    unconditionally.

    🔴 **And no `search_queries` row, on either tier or either arm** — argued
    rather than deferred (F2). `search_queries.mode` is a `SearchMode`, three
    reachable values; a tier is a disjoint vocabulary, so storing both under
    one column is two vocabularies under one name. And this route is driven
    per keystroke at tier 1's p50 of 0.6 ms against full text's 33.3 ms, so its
    rows would out-number *and* out-weight the searches by an order of
    magnitude each in every mode-split panel PRD 10 builds. What it costs is
    that the question PRD 10 most wants that table for — whether real users
    type two- to four-character queries at all — is a question about *this* box, and
    cannot be answered in M9. The two amendments that would answer it are
    named in PRD 10; neither is a decision this route may take on its own,
    exactly as with the problem code above.
    """
    minimum = _MIN_CHARS_FOR_TIER[tier]
    if len(q.strip()) < minimum:
        return SuggestResponse.of(q, tier=tier, min_query_length=minimum)
    return SuggestResponse.of(
        q,
        tier=tier,
        min_query_length=minimum,
        # `tier=tier` and not a tier the service chose: the echo has to be the
        # parameter that selected the index, or a response could report a tier
        # that did not run.
        results=await search_service.suggest(q, limit=limit, tier=tier),
    )
