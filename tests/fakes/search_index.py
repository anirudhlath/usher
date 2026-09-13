"""In-memory `SearchIndex` and `SuggestIndex`."""

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
        population = self._population(request.filters)
        lexical = _rank(
            (
                (document, score)
                for document in population
                if (score := _text_score(document, _terms(request.query))) > 0.0
            ),
            # The lexical lane is the only one with a typed string to compare a
            # name against, which is `PostgresSearchIndex`'s own split: the
            # vector lane matches an embedding and is handed no query text at
            # all (issue #25).
            query=request.query,
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

    async def semantic_coverage(self, filters: SearchFilters) -> float:
        # `_coverage` over `_population`, exactly as `search` above -- one
        # definition, so the probe and the answer cannot drift apart here any
        # more than they can on Postgres. `_passes` raises `FilterNotSupported`
        # for the two members this class cannot express, which is the port's
        # rule and is what stops a caller reading "cannot ask" as "empty lane".
        return _coverage(self._population(filters))

    def _population(self, filters: SearchFilters) -> list[SearchDocument]:
        return [
            document for document in self._documents.values() if self._passes(document, filters)
        ]

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

    def given(
        self, *, name: str, popularity: float = 1.0, title_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """Test-only writer, deliberately absent from the port.

        `SuggestIndex` has no write method and `PostgresSuggestIndex` writes
        nothing at all -- it reads `titles`. Adding `index`/`remove` to the
        port so this class could implement them is exactly the change
        ADR-0021 exists to make visible, so the seam stays here, in
        `tests/`, where nothing in `src/` can reach it.

        **`title_id` is optional so two tiers can be seeded over one
        catalog.** A case that asks *which tier answered* has to hand the same
        row to both doubles and to the `TitleRepository` the hydration reads
        through; minting an id here would make the three disagree and the
        hydration would drop every hit, which is a green empty box for the
        wrong reason.
        """
        title_id = new_id() if title_id is None else title_id
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


class FakePrefixSuggestIndex(SuggestIndex):
    """Tier 1's matching rule and nothing else: the name starts with the typed prefix.

    **Subclasses no contract, deliberately**, for the reason this module's
    docstring gives: checked against `SuggestIndexContract` it would be
    `str.startswith` asserting against `str.startswith`, and the real tier's
    cases are about which index Postgres takes. What it is for is a case that
    has to tell **which tier answered** -- it finds no typo where
    `FakeSuggestIndex` finds one, and that disagreement is the only thing a
    tier selector can be held to.

    Two further divergences from `PostgresPrefixSuggestIndex`, both in the
    forgiving direction and neither reachable by anything above the port.
    There is no `LIKE` escaping here, so nothing says what a typed `%` costs;
    and the ordering is `popularity DESC, id` where the real statement is
    `popularity DESC NULLS LAST, vote_count DESC NULLS LAST, id ASC`, because
    a `SuggestIndex` hands back ids and a `vote_count` is not one of them.
    """

    def __init__(self) -> None:
        self._names: dict[uuid.UUID, tuple[str, float]] = {}

    def given(
        self, *, name: str, popularity: float = 1.0, title_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """Test-only writer, on `FakeSuggestIndex.given`'s terms exactly."""
        title_id = new_id() if title_id is None else title_id
        self._names[title_id] = (name, popularity)
        return title_id

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        # The real statement's own guard, mirrored: an empty box is the state
        # of every page load and `LIKE '%'` is a whole-catalog sort for a
        # question nobody asked. Without it this double would answer the whole
        # dict for `""`, which is the one behaviour tier 1 provably does not
        # have.
        if not prefix.strip():
            return []
        wanted = prefix.casefold()
        matched = [
            (popularity, title_id)
            for title_id, (name, popularity) in self._names.items()
            if name.casefold().startswith(wanted)
        ]
        matched.sort(key=lambda row: (-row[0], row[1].bytes))
        # 1.0 for every row, which is the shipped tier's own answer and its
        # own reason: every row is an exact prefix match, so the distance
        # tier 2 varies its score with is zero for all of them.
        return [SearchHit(title_id=title_id, score=1.0) for _, title_id in matched[: max(limit, 0)]]


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


def _rank(
    scored: Iterable[tuple[SearchDocument, float]], *, query: str | None = None
) -> list[SearchHit]:
    # An exact name match leads, then score descending, then popularity descending, then
    # id.
    ordered = sorted(
        scored,
        key=lambda pair: (
            not _is_exact_name(pair[0], query),
            -pair[1],
            -(pair[0].popularity or 0.0),
            pair[0].title_id.bytes,
        ),
    )
    return [
        SearchHit(
            title_id=document.title_id,
            score=score,
            exact_name=_is_exact_name(document, query),
        )
        for document, score in ordered
    ]


def _is_exact_name(document: SearchDocument, query: str | None) -> bool:
    """Python's `casefold()` where the statement spells `lower(t.name) = lower(btrim(...))`.

    the divergence this module's docstring already records for `FakeSuggestIndex`, in a
    second place.

    The two agree on ASCII and no case in this repository names a title in anything
    else.
    """
    return query is not None and document.name.casefold() == query.strip().casefold()


def _fuse(*lanes: Sequence[SearchHit]) -> list[SearchHit]:
    """Reciprocal rank fusion, by **rank**.

    Never a sum of the lanes' own scores: a cosine and a `ts_rank` are not
    on the same scale, and adding them makes whichever lane happens to emit
    larger numbers the only lane that matters. ADR-0002 says so; the
    contract's `test_fusion_does_not_add_scores_from_different_scales` is
    what would catch this function being "simplified" into addition.
    """
    scores: dict[uuid.UUID, float] = {}
    # Carried from whichever lane knew, which is the lexical one. A fused
    # answer that dropped it would leave `_dense_ranks` with nothing to
    # separate an exact name match from the rows tied to it, on the one mode
    # where both lanes ran (issue #25).
    exact: set[uuid.UUID] = set()
    for lane in lanes:
        for rank, hit in enumerate(lane):
            scores[hit.title_id] = scores.get(hit.title_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            if hit.exact_name:
                exact.add(hit.title_id)
    ordered = sorted(
        scores.items(), key=lambda pair: (pair[0] not in exact, -pair[1], pair[0].bytes)
    )
    return [
        SearchHit(title_id=title_id, score=score, exact_name=title_id in exact)
        for title_id, score in ordered
    ]


def _coverage(population: Sequence[SearchDocument]) -> float:
    if not population:
        return 0.0
    return sum(1 for document in population if document.vector is not None) / len(population)


def _edit_distance(left: str, right: str) -> int:
    """Plain Levenshtein.

    Not Damerau: a transposition costs 2 here, which is what
    `TypoTolerantSuggestIndexContract`'s transposition case is arranged for.
    """
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
