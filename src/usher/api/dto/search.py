"""`GET /search` — PRD 05's read path on the wire, and PRD 07's `### Screens`.

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

from pydantic import BaseModel

from usher.domain.enums import TitleKind
from usher.domain.search import SearchResult
from usher.ports.search import SearchMode
from usher.services.search import SearchAnswer


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
    """

    query: str
    requested_mode: SearchMode
    mode: SearchMode
    semantic_coverage: float
    expanded_query: str | None
    results: tuple[SearchResultResponse, ...]

    @classmethod
    def of(cls, query: str, answer: SearchAnswer) -> "SearchResponse":
        return cls(
            query=query,
            requested_mode=answer.requested_mode,
            mode=answer.mode,
            semantic_coverage=answer.semantic_coverage,
            expanded_query=answer.expanded_query,
            results=tuple(SearchResultResponse.of(result) for result in answer.results),
        )


__all__ = ["SearchResponse", "SearchResultResponse"]
