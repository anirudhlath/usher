"""`GET /search` and `GET /search/suggest` — PRD 05's read path on the wire,
and PRD 07's `### Screens`.

**`SearchMode` is the port enum, not a wire twin**, for the reason
`dto/home.py` reuses the domain `DisplayHint`: this route has a
`response_model`, so `/openapi.json` describes the vocabulary and a rename is a
visible schema diff *plus* a mypy error here. PRD 07's sketch spells the
parameter `semantic=` and that is a **boolean**, which cannot express `fused`
at all — so the sketch is corrected in the same commit rather than shipped
beside the enum. Two vocabularies for one field is worse than the rename:
`?semantic=true&mode=full_text` has no answer anybody would agree on.

**Absence is not this module's convention.** `dto/title.py` uses an absent key
for every empty value, and `expanded_query` here is deliberately
present-and-null — the two are different questions. An absent `images` says
"this server does not have that capability yet"; a null `expanded_query` says
"nothing was substituted on this search", which is a *fact about this request*
that a client renders (or declines to render) on every response. A field that
appeared only when it was populated would make "was my query rewritten?"
answerable only by a key test, on the one surface where the answer changes what
the viewer is looking at.

**No source concept, no `external_id`, no credential.** PRD 07's first line.
`SearchResult` carries none to begin with: a hit names a `Title.id`, which is
every route a client can call.
"""

import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from usher.domain.enums import TitleKind
from usher.domain.search import SearchResult
from usher.ports.search import SearchMode
from usher.services.search import SearchAnswer, SuggestTier


class SearchResultResponse(BaseModel):
    """One ranked hit, hydrated.

    `score` is the **blended** score `SearchService._blend` produced, not the
    index's — comparable within one answer and meaningless between two, because
    the relevance term is derived from position within the candidate set this
    request returned. Named `score` rather than `relevance` for exactly that
    reason, following the domain model it renders.

    `owned` rides along because PRD 05 requires unowned results to be surfaced
    "clearly marked": a client that had to ask a second question to render the
    badge would either ask it per row or not render it.

    `popularity` is nullable and stays nullable — it is `null` for every title
    TMDb's daily export has never described, which is ~77% of a fully
    bootstrapped catalog and **all** of an IMDb-only one. `popularity or 0.0`
    here would render "nobody has measured this" identically to "measured, and
    unpopular" (ADR-0014).
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    popularity: float | None
    owned: bool
    score: float

    @classmethod
    def of(cls, result: SearchResult) -> "SearchResultResponse":
        return cls(
            title_id=result.title_id,
            kind=result.kind,
            name=result.name,
            year=result.year,
            popularity=result.popularity,
            owned=result.owned,
            score=result.score,
        )


class SearchResponse(BaseModel):
    """The ranked results, plus what actually ran.

    **`requested_mode` beside `mode` is the degradation made visible**, and it
    is the whole reason two fields exist where one would render. A `fused`
    request on a deployment with no embedding model is served as full text and
    every row of that answer is correct — so with one field the only signal is
    `semantic_coverage == 0.0`, which is *also* what a healthy fused search over
    a catalog with no embeddings reports. Two different problems with two
    different fixes (install the `embedding` extra; run `usher index
    --backfill`) that would otherwise present identically. They are equal on
    every undegraded search, which is what makes the inequality readable.

    **`expanded_query` is the substitution made visible**, one field over and on
    the same argument. When an LLM rewrote the query this is exactly the text
    the semantic lane embedded; `null` means the vector came from the query as
    typed. Without it a viewer searches for one thing, gets results for another,
    and has nothing to say so — and cannot tell a good expansion from a bad one,
    which is also the first thing an operator reading their bug report needs.

    ⚠️ **The implication runs one way only, and the biconditional is false.** A
    populated `expanded_query` means a completion was bought; a `null` one means
    **nothing about spend**. A call that answers with the wrong key is billed in
    full — real tokens, a real cost, one `llm_calls` row with `ok = false` — and
    still leaves this `null`, as does an unreachable endpoint and a rewrite that
    came back blank or over-long. `llm_calls` is where spend is legible. It is
    `null` on every path that embedded the query as typed: the shipped default
    with expansion off, a `full_text` search, a blank query, a deployment with
    no embedder, and a failed or unusable expansion.

    **`semantic_coverage` is `SearchOutcome`'s number, passed through and never
    recomputed** from the results below it. It is the fraction of the *filtered
    population* that had a vector; derived from the returned hits it would read
    `1.0` exactly whenever every hit happened to have one, which is precisely
    what a green test seeds. A `full_text` request reports `0.0` because no
    semantic lane ran — that is a statement about the request and not about the
    catalog, and a client must ask for `semantic` or `fused` if that is the
    question.

    **`query` is echoed as typed**, never the rewrite: it is what the lexical
    lane matched on and what a client renders above the results. The pair
    (`query`, `expanded_query`) is what makes an expansion legible; one field
    carrying whichever of the two happened to be embedded would make it
    invisible.

    **`search_id` is opaque and is the only thing on this response a client is
    asked to hand back.** It names the `search_queries` row this answer was
    recorded as, and its two uses are `GET /titles/{id}?search_id=…` — which
    records *which result was opened* — and `POST /titles/{id}/play` (or
    `/episodes/{id}/play`) — which records *that one was played*. Together
    those fill the two columns PRD 10 says the table cannot ship without.

    ⚠️ **Opaque means opaque, and the shape invites the opposite.** It is a
    UUIDv7 and therefore carries a timestamp and sorts, so a client could read
    a search's time out of it or order two of them — and neither is a promise.
    Nothing else may be inferred: it is not the household, not the query, and
    not a handle any other route accepts.

    **`null` is a fact about this deployment rather than about the search**,
    and it is not an error: no row was written, so there is nothing to attach
    an outcome to. Three ways to get one, all of them PRD 10's own list — a
    blank query, a search with no household, and a deployment that composed
    `SearchService` with no analytics — plus a write the store refused, which
    answers `null` for the same reason. A client sees the same complete,
    correct results either way and simply has nothing to report back, so
    **omitting the parameter is always legal** and never changes a response.
    """

    query: str
    requested_mode: SearchMode
    mode: SearchMode
    semantic_coverage: float
    expanded_query: str | None
    search_id: uuid.UUID | None
    results: tuple[SearchResultResponse, ...]

    @classmethod
    def of(cls, query: str, answer: SearchAnswer) -> "SearchResponse":
        return cls(
            query=query,
            requested_mode=answer.requested_mode,
            mode=answer.mode,
            semantic_coverage=answer.semantic_coverage,
            expanded_query=answer.expanded_query,
            search_id=answer.search_id,
            results=tuple(SearchResultResponse.of(result) for result in answer.results),
        )


class SuggestResultResponse(BaseModel):
    """One type-ahead candidate, hydrated.

    **The same six fields as `SearchResultResponse` and a different `score`,
    which is why it is a second model rather than a reuse.** That one renders
    `SearchService._blend`'s weighted mean over six ranking terms; this one
    renders the index's own rank-shaped value, because `suggest` is
    deliberately **not re-ranked** — the tier already ordered its own answer
    and applying the blend on top would count popularity twice.

    ⚠️ **`score` is not comparable across tiers, and this is the trap.** Tier 1
    answers **1.0 for every row**, honestly: every row is an exact prefix match
    so the distance tier 2 varies its score with is zero for all of them. Tier
    2 answers `1 / (1 + edit distance)`. A client that painted tier 1 and then
    replaced the box with tier 2 sees every score fall, and that is a change of
    *scale*, not of quality. Render the order, not the number. Within one
    answer it is a rank; between two answers of different tiers it means
    nothing at all.

    `owned` rides along for `SearchResultResponse`'s reason: PRD 05 requires
    unowned results to be surfaced "clearly marked", and a type-ahead box is
    the surface most likely to skip a second request per row to find out.

    `popularity` is nullable and stays nullable (ADR-0014) — `null` for every
    title TMDb's daily export has never described, which is **all** of an
    IMDb-only catalog and is exactly the population whose tier-1 ordering falls
    through to `vote_count`.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    popularity: float | None
    owned: bool
    score: float

    @classmethod
    def of(cls, result: SearchResult) -> "SuggestResultResponse":
        return cls(
            title_id=result.title_id,
            kind=result.kind,
            name=result.name,
            year=result.year,
            popularity=result.popularity,
            owned=result.owned,
            score=result.score,
        )


class SuggestResponse(BaseModel):
    """The type-ahead box, plus which tier filled it and what it refuses.

    **`tier` is the echo, and it is `requested_mode`'s argument minus the
    degradation.** `GET /search` carries two mode fields because a `fused`
    request can be *served* narrower; a tier request is always served by the
    tier it named, because both indexes exist on every deployment `m09a`
    reaches — so there is one field here rather than two, and a second one
    would be a value that could never differ. The echo is still owed for a
    different reason: **`?tier=` has a default**, so a client that named no
    tier is reading an answer from a tier it did not choose, and the two tiers
    give *different answers to the same `q`* by design. A response that did not
    say which is uninterpretable beside another one, and ADR-0031 records
    changing the default as a live possibility.

    **`min_query_length` is what makes an empty box legible**, and it is the
    only thing that can. Below it this route runs no query at all, so
    `results: []` would otherwise be indistinguishable from *"no title starts
    with that"* — a filter with no counter, which is the failure
    `.claude/rules/ports-and-error-taxonomy.md` records as surviving every
    test because nothing that is missing raises anything. It is a fact about
    the tier that answered, present on every response rather than only on the
    refusing ones, so a client can implement the same rule locally and stop
    sending the request: on tier 1 that is worth **2,707 ms of database work at
    one character** (ADR-0031's curve).

    **`query` is echoed as typed** — not stripped, not lower-cased. It is what
    the `LIKE` pattern was built from, and a client rendering "no matches for
    …" needs the string the server actually used.
    """

    query: str
    tier: SuggestTier
    min_query_length: int
    results: tuple[SuggestResultResponse, ...]

    @classmethod
    def of(
        cls,
        query: str,
        *,
        tier: SuggestTier,
        min_query_length: int,
        results: Sequence[SearchResult] = (),
    ) -> "SuggestResponse":
        return cls(
            query=query,
            tier=tier,
            min_query_length=min_query_length,
            results=tuple(SuggestResultResponse.of(result) for result in results),
        )


__all__ = [
    "SearchResponse",
    "SearchResultResponse",
    "SuggestResponse",
    "SuggestResultResponse",
]
