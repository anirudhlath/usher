"""In-memory `SearchIndex` and `SuggestIndex`.

**Where `FakeSearchIndex` is more forgiving than Postgres, on purpose. Six
places, and the first is not a nuance -- it is most of what a search engine
is:**

- **It has no text analysis at all.** Substring matching over casefolded
  fields: no stemming, no stop words, no `tsquery` parsing, no phrase
  handling, no `ts_rank` and no length normalisation. So "searching for
  *running* finds *run*", "`the` matches nothing", and every real ranking
  property are invisible from here. `PostgresSearchIndex` runs the identical
  contract with the real analyzer.
- **Its weight classes are four hand-coded constants**, not `setweight` plus
  `ts_rank`'s own normalisation. `test_a_name_match_outranks_an_overview_
  match` therefore proves the *fake* is weighted; that the shipped index is
  weighted is proved by the same case against real Postgres.
- **Idempotence is free.** It is a dict keyed by `title_id`, so an
  `index_many` that appends is structurally impossible here. The case exists
  for the real one, where an `INSERT` without `ON CONFLICT` is one keystroke
  away.
- **No dimension check.** `SearchDocument.vector` may be any width; the real
  column is `halfvec(384)` and rejects anything else. There is also no
  quantisation, so a vector round-trips exactly where Postgres loses float16
  precision (measured max cosine error 1.21e-04).
- **`semantic_coverage` is exact**, computed over the whole filtered
  population because the whole filtered population is in memory. A real
  implementation computes it over a candidate window and may approximate.
- **It cannot fail.** No connection, no lock, no index build cost, no
  timeout, no `PortUnavailable`. Nothing here exercises a single error path.

**`FakeSuggestIndex` doubles for the typo-tolerant tier and there is no
double for the other one.** M9's two-tier suggest ships a second
`SuggestIndex` -- `PostgresPrefixSuggestIndex`, a btree prefix probe whose
measured typo recall is 1.9% -- and this class subclasses
`TypoTolerantSuggestIndexContract`, i.e. the trigram path's contract. An
in-memory prefix double would be `str.startswith` asserting against
`str.startswith`; what that tier's cases are actually about is which index
Postgres takes, so its arm is integration-only by construction.

**Where `FakeSuggestIndex` is more forgiving, on purpose. Four places:**

- **No candidate cap.** It computes edit distance over its whole dict, so
  the one property the real path exists for is structurally absent -- and its
  typo tolerance is therefore *better* than the real one, which is the
  dangerous direction. `TypoTolerantSuggestIndexContract`'s cap case is
  skipped here by capability flag rather than passed.
- **Levenshtein only**, with no trigram pre-filter and therefore none of
  `pg_trgm`'s similarity threshold or its recall cliff on short names.
- **`given()` is a test-only writer.** The port has no write method
  (ADR-0021) and neither real implementation writes anything at all, so the
  one fact this class *cannot* model is the absence it exists to stand in
  for.
- **Python's `casefold()`, not Postgres's `lower()`** and not its collation,
  so nothing here says anything about non-ASCII names.
"""

import uuid
from collections.abc import Iterable, Sequence

from usher.domain.ids import new_id
from usher.ports.search import (
    FilterNotSupported,
    SearchDocument,
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SuggestIndex,
)

# Standard reciprocal-rank-fusion constant. The contract's two fusion cases
# are arranged with a 2x margin at this value, so they do not become
# assertions about the constant.
_RRF_K = 60

# Weight classes, mirroring PRD 05's ordering: names, then credits (class B,
# reserved and empty in M6 -- boundary call 2), then genres and keywords,
# then the long prose. Constants rather than `setweight`, which is the
# second divergence in this module's docstring.
_NAME_WEIGHT = 1.0
_CREDIT_WEIGHT = 0.4
_TAG_WEIGHT = 0.2
_PROSE_WEIGHT = 0.1


class FakeSearchIndex(SearchIndex):
    def __init__(self) -> None:
        self._documents: dict[uuid.UUID, SearchDocument] = {}

    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        for document in documents:
            self._documents[document.title_id] = document

    async def remove(self, title_id: uuid.UUID) -> None:
        # Text and vector together -- the document *is* both. A real
        # implementation has two places to forget.
        self._documents.pop(title_id, None)

    async def search(self, request: SearchRequest) -> SearchOutcome:
        population = [
            document
            for document in self._documents.values()
            if self._passes(document, request.filters)
        ]
        lexical = _rank(
            (document, score)
            for document in population
            if (score := _text_score(document, _terms(request.query))) > 0.0
        )
        vectors = _rank(
            (document, _dot(document.vector, request.query_vector))
            for document in population
            # `is not None`, never a truthiness test and never a fallback to
            # zeros: a title with no vector is not a candidate, which is the
            # difference between "absent" and "a mediocre match for
            # everything".
            if document.vector is not None and request.query_vector is not None
        )
        coverage = _coverage(population)
        hits: list[SearchHit]
        match request.mode:
            case SearchMode.FULL_TEXT:
                # 0.0 rather than the measured fraction: no semantic lane
                # ran, and reporting coverage for a lane that did not run
                # invites a caller to read it as a fact about the catalog.
                hits, coverage = lexical, 0.0
            case SearchMode.SEMANTIC:
                hits = vectors
            case SearchMode.FUSED:
                hits = _fuse(lexical, vectors)
        return SearchOutcome(hits=tuple(hits[: max(request.limit, 0)]), semantic_coverage=coverage)

    def _passes(self, document: SearchDocument, filters: SearchFilters) -> bool:
        # Both refusals are honest rather than defensive: `owned_only` is a
        # fact about `media_items` and `min_enrichment` is a fact about
        # `titles.enrichment_state`, and this class holds neither table.
        # Returning everything instead would be the "an ignored filter
        # returns more rows" failure the port's docstring forbids.
        if filters.owned_only:
            raise FilterNotSupported("owned_only")
        if filters.min_enrichment is not None:
            raise FilterNotSupported("min_enrichment")
        if filters.kinds and document.kind not in filters.kinds:
            return False
        if filters.genres and not set(filters.genres) & set(document.genres):
            return False
        if filters.year_from is not None and (
            document.year is None or document.year < filters.year_from
        ):
            return False
        return not (
            filters.year_to is not None
            and (document.year is None or document.year > filters.year_to)
        )


class FakeSuggestIndex(SuggestIndex):
    def __init__(self, *, max_distance: int = 2) -> None:
        self._names: dict[uuid.UUID, tuple[str, float]] = {}
        self._max_distance = max_distance

    def given(self, *, name: str, popularity: float = 1.0) -> uuid.UUID:
        """Test-only writer, deliberately absent from the port.

        `SuggestIndex` has no write method and `PostgresSuggestIndex` writes
        nothing at all -- it reads `titles`. Adding `index`/`remove` to the
        port so this class could implement them is exactly the change
        ADR-0021 exists to make visible, so the seam stays here, in
        `tests/`, where nothing in `src/` can reach it.
        """
        title_id = new_id()
        self._names[title_id] = (name, popularity)
        return title_id

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        wanted = prefix.casefold()
        scored: list[tuple[int, float, uuid.UUID]] = []
        for title_id, (name, popularity) in self._names.items():
            head = name.casefold()[: len(wanted)]
            distance = 0 if name.casefold().startswith(wanted) else _edit_distance(wanted, head)
            if distance <= self._max_distance:
                scored.append((distance, popularity, title_id))
        # Distance ascending, then popularity descending -- the type-ahead
        # box's first row must not be arbitrary among equally-good matches.
        # `title_id.bytes` last so the order is total and a tie cannot come
        # back differently on two runs.
        scored.sort(key=lambda row: (row[0], -row[1], row[2].bytes))
        return [
            SearchHit(title_id=title_id, score=1.0 / (1.0 + distance))
            for distance, _, title_id in scored[: max(limit, 0)]
        ]


def _terms(query: str) -> list[str]:
    return [term for term in query.casefold().split() if term]


def _text_score(document: SearchDocument, terms: Sequence[str]) -> float:
    classes: tuple[tuple[float, tuple[str, ...]], ...] = (
        (_NAME_WEIGHT, (document.name, document.original_name or "", document.sort_name)),
        (_CREDIT_WEIGHT, document.credits),
        (_TAG_WEIGHT, document.genres + document.keywords),
        (_PROSE_WEIGHT, (document.overview or "", document.tagline or "")),
    )
    score = 0.0
    for weight, fields in classes:
        haystack = " ".join(fields).casefold()
        if any(term in haystack for term in terms):
            score += weight
    return score


def _dot(left: tuple[float, ...] | None, right: tuple[float, ...] | None) -> float:
    assert left is not None and right is not None
    # `strict=True`: a width mismatch is a bug worth raising over, and this
    # is the only dimension check anywhere in this module.
    return sum(one * other for one, other in zip(left, right, strict=True))


def _rank(scored: Iterable[tuple[SearchDocument, float]]) -> list[SearchHit]:
    # Score descending, then popularity descending, then id. The popularity
    # key is what makes the weight-class case's distractor bite: without it
    # an unweighted implementation ties and the case coin-flips into a pass.
    # The id key makes the order total, so a tie cannot come back
    # differently on two runs.
    ordered = sorted(
        scored, key=lambda pair: (-pair[1], -(pair[0].popularity or 0.0), pair[0].title_id.bytes)
    )
    return [SearchHit(title_id=document.title_id, score=score) for document, score in ordered]


def _fuse(*lanes: Sequence[SearchHit]) -> list[SearchHit]:
    """Reciprocal rank fusion, by **rank**.

    Never a sum of the lanes' own scores: a cosine and a `ts_rank` are not
    on the same scale, and adding them makes whichever lane happens to emit
    larger numbers the only lane that matters. ADR-0002 says so; the
    contract's `test_fusion_does_not_add_scores_from_different_scales` is
    what would catch this function being "simplified" into addition.
    """
    scores: dict[uuid.UUID, float] = {}
    for lane in lanes:
        for rank, hit in enumerate(lane):
            scores[hit.title_id] = scores.get(hit.title_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0].bytes))
    return [SearchHit(title_id=title_id, score=score) for title_id, score in ordered]


def _coverage(population: Sequence[SearchDocument]) -> float:
    if not population:
        return 0.0
    return sum(1 for document in population if document.vector is not None) / len(population)


def _edit_distance(left: str, right: str) -> int:
    """Plain Levenshtein. Not Damerau: a transposition costs 2 here, which
    is what `TypoTolerantSuggestIndexContract`'s transposition case is
    arranged for."""
    previous = list(range(len(right) + 1))
    for row, one in enumerate(left, start=1):
        current = [row]
        for column, other in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (one != other),
                )
            )
        previous = current
    return previous[-1]
