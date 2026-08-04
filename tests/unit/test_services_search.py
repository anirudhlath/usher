"""`SearchService`'s ranking, its degradation, and who embeds.

**Retrieval is held fixed here and only ranking varies**, which is why these
cases drive a scripted `SearchIndex` rather than `FakeSearchIndex`. That fake
is the contract-tested double for the *port* and its own docstring says it has
no text analysis at all -- no stemming, no `tsquery`, no weight classes -- so
a ranking assertion driven through its matching would be an assertion about a
tokenizer nobody shipped. `SearchIndexContract` covers the port; this file
covers what the service does with what the port returned.

**Every id below is a fixed `uuid.UUID(int=...)` rather than a `new_id()`, and
that is load-bearing rather than tidy.** Several of the mutations this file
exists to kill collapse two rows onto the same blended score, at which point
the deterministic tiebreak decides the order -- so a case can only *see* the
mutation if it knows which of its two rows the tiebreak would pick. With
random UUIDv7s the same mutation would pass or fail depending on the minute
the suite ran.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tests.fakes.embedding import FakeEmbedder
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert
from usher.ports.search import (
    SearchDocument,
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SuggestIndex,
)
from usher.services.search import SearchService, SemanticSearchUnavailable

# Fixed ids, ordered on purpose. `_UNOWNED < _OWNED` and `_ZERO_POP <
# _NO_POP` so that a mutation which ties the two rows sorts the *wrong* one
# first and the case goes red; `_LOW_ID < _HIGH_ID` is the tiebreak case's
# whole content.
_QUIET = uuid.UUID(int=0x01)
_POPULAR = uuid.UUID(int=0x02)
_UNOWNED = uuid.UUID(int=0x03)
_OWNED = uuid.UUID(int=0x04)
_ZERO_POP = uuid.UUID(int=0x05)
_NO_POP = uuid.UUID(int=0x06)
_LOW_ID = uuid.UUID(int=0x07)
_HIGH_ID = uuid.UUID(int=0x08)
_DELETED = uuid.UUID(int=0x09)
_SOURCE = uuid.UUID(int=0xFF)

# A `ts_rank` lands around 0.06 and an RRF score around 0.016-0.033. The two
# scores below are on that scale rather than on [0, 1], and the difference is
# not cosmetic: the mutation `relevance=hit.score` **survives** a case whose
# raw scores are 0.9 against 0.1, because at that magnitude the raw score is
# already larger than any popularity term and the wrong implementation still
# orders correctly. Realistic magnitudes are what make the incompatible-scale
# failure visible at all.
_STRONG = 0.06
_WEAK = 0.02

_SEEN_AT = datetime(2026, 8, 2, tzinfo=UTC)

_CATALOG: dict[uuid.UUID, tuple[str, float | None]] = {
    _QUIET: ("The Quiet Vacuum", 0.5),
    _POPULAR: ("Vacuum Sales Quarterly", 1000.0),
    _UNOWNED: ("A Vacuum in Winter", None),
    _OWNED: ("Vacuum Chamber Diaries", None),
    _ZERO_POP: ("Vacuum, Measured", 0.0),
    _NO_POP: ("Vacuum, Undescribed", None),
    _LOW_ID: ("Vacuum Alpha", None),
    _HIGH_ID: ("Vacuum Omega", None),
}


class _ScriptedIndex(SearchIndex):
    """A `SearchIndex` that returns exactly what a case tells it to.

    Records the request it was handed, so the "who embeds" cases can assert on
    the vector that crossed the port rather than on the vector the service
    happened to compute.
    """

    def __init__(self, outcome: SearchOutcome) -> None:
        self.outcome = outcome
        self.requests: list[SearchRequest] = []

    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        return None

    async def remove(self, title_id: uuid.UUID) -> None:
        return None

    async def search(self, request: SearchRequest) -> SearchOutcome:
        self.requests.append(request)
        return self.outcome


class _ScriptedSuggest(SuggestIndex):
    """A `SuggestIndex` whose order is the thing under test.

    The hits it returns are deliberately in an order the search blend would
    *not* produce, so "the service left the order alone" and "the service
    re-ranked" are distinguishable.
    """

    def __init__(self, hits: Sequence[SearchHit] = ()) -> None:
        self.hits = list(hits)
        self.calls: list[tuple[str, int]] = []

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        self.calls.append((prefix, limit))
        return list(self.hits[:limit])


def _title(title_id: uuid.UUID) -> Title:
    name, popularity = _CATALOG[title_id]
    return Title(
        id=title_id,
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name.casefold(),
        year=2019,
        popularity=popularity,
    )


def _copy(title_id: uuid.UUID) -> MediaItemUpsert:
    """One `media_items` row for a title itself, not for one of its episodes.

    `episode_id=None` is the clause `owned_title_ids` carries. The retracted
    half of the definition -- `available = false` still counts -- is asserted
    against real Postgres, where the sweep that sets it lives.
    """
    return MediaItemUpsert(
        source_id=_SOURCE,
        external_id=str(title_id),
        title_id=title_id,
        episode_id=None,
        container=None,
        video_codec=None,
        audio_codec=None,
        width=None,
        height=None,
        hdr_format=None,
        audio_channels=None,
        file_size_bytes=None,
        runtime_seconds=None,
        added_at=None,
        last_seen_at=_SEEN_AT,
    )


async def _service(
    index: SearchIndex,
    *,
    embedder: FakeEmbedder | None = None,
    owned: frozenset[uuid.UUID] = frozenset(),
    suggestions: SuggestIndex | None = None,
    result_limit: int = 50,
) -> SearchService:
    """The service over fakes, with the whole invented catalog already stored.

    Seeding every title rather than only the ones a case names keeps the
    hydration read honest: an implementation that returned rows the index never
    mentioned would have somewhere to get them from.
    """
    titles = FakeTitleRepository()
    media_items = FakeMediaItemRepository()
    for title_id in _CATALOG:
        await titles.add(_title(title_id))
    if owned:
        await media_items.upsert_many([_copy(title_id) for title_id in sorted(owned)])
    return SearchService(
        index,
        _ScriptedSuggest() if suggestions is None else suggestions,
        titles,
        media_items,
        result_limit=result_limit,
        embedder=embedder,
    )


# --- who embeds, and with what --------------------------------------------


async def test_the_service_embeds_the_query_and_the_index_never_does() -> None:
    """Defect 4 of the search port's own 🔶, from the caller's side. Fails: an
    index that embeds for itself -- a backend with its own model has its own
    prefix convention, its own checkpoint and its own drift, and nothing above
    it can see any of the three."""
    embedder = FakeEmbedder()
    index = _ScriptedIndex(SearchOutcome())
    service = await _service(index, embedder=embedder)
    await service.search("an empty room", mode=SearchMode.SEMANTIC)
    assert index.requests[0].query_vector == tuple((await embedder.embed(["an empty room"]))[0])


async def test_the_query_is_embedded_exactly_as_typed_with_no_instruction_prefix() -> None:
    """**The embedding port's 🔶 3 settlement, asserted where a caller could
    break it.** `ports/embedding.py` used to say "callers are responsible for
    any query-side instruction prefix" and this service is the caller it meant.
    The documented BGE prefix moves MRR -0.0028, CI [-0.0259, +0.0203]; applied
    to both sides -- which one symmetric loop plus that instruction produces --
    it is -0.0663, CI [-0.1013, -0.0330]. `compose_document` applies none on
    the document side, so anything added here is one of those two conditions
    and nothing else in this repository could detect it."""
    embedder = FakeEmbedder()
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=embedder)
    await service.search("an empty room", mode=SearchMode.SEMANTIC)
    assert embedder.calls == [["an empty room"]]


async def test_a_blank_query_never_reaches_the_model() -> None:
    """The degenerate-document trap, on the query side. Every whitespace-only
    input embeds to the *identical* vector -- cos = 1.0000 exactly -- and that
    vector is perfectly valid, so a blank semantic query returns a confident
    ranked list of whatever sits nearest a degenerate point, with no error and
    no empty result to say the query was empty. `compose_document` refuses a
    degenerate document; this refuses a degenerate query. Empty rather than a
    raise: a search box sends this between keystrokes."""
    embedder = FakeEmbedder()
    index = _ScriptedIndex(SearchOutcome(hits=(SearchHit(title_id=_QUIET, score=1.0),)))
    service = await _service(index, embedder=embedder)
    answer = await service.search("   ", mode=SearchMode.SEMANTIC)
    assert answer.results == ()
    assert embedder.calls == []
    assert index.requests == [], "a blank query reached the index"


async def test_a_blank_prefix_never_reaches_the_suggest_index() -> None:
    """The same refusal on the type-ahead path, which is where a search box
    actually sends whitespace. Fails: a `suggest` that forwards it, which on
    the real backend is a trigram probe with an empty needle against every
    name in a 1,271,138-row table."""
    suggestions = _ScriptedSuggest((SearchHit(title_id=_QUIET, score=1.0),))
    service = await _service(_ScriptedIndex(SearchOutcome()), suggestions=suggestions)
    assert await service.suggest("  ") == ()
    assert suggestions.calls == []


# --- degradation -----------------------------------------------------------


async def test_semantic_with_no_embedder_is_an_error_not_a_full_text_fallback() -> None:
    """Fails: a silent fallback to full-text, which returns a plausible ranked
    list for a query whose whole point was that full-text could not answer it
    ("movies about isolation in space" against a `tsquery` for four words).
    PRD 08's rule is that a degraded subsystem *narrows*; SEMANTIC minus its
    only lane is not narrower, it is a different question answered without
    saying so."""
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=None)
    with pytest.raises(SemanticSearchUnavailable):
        await service.search("an empty room", mode=SearchMode.SEMANTIC)


async def test_fused_with_no_embedder_narrows_to_full_text_and_says_which() -> None:
    """The other half of the asymmetry, and the half that must not raise --
    FUSED minus its semantic lane is still full-text, an honest answer local
    state can give. Two wrong implementations; the second is the dangerous
    one. Raising fails a request local state can answer. **Degrading silently**
    returns the right rows under the label the caller asked for, so an operator
    running `--mode fused` without the embedding extra sees a working hybrid
    search forever."""
    index = _ScriptedIndex(SearchOutcome(hits=(SearchHit(title_id=_QUIET, score=1.0),)))
    service = await _service(index, embedder=None)
    answer = await service.search("vacuum", mode=SearchMode.FUSED)
    assert answer.requested_mode is SearchMode.FUSED
    assert answer.mode is SearchMode.FULL_TEXT
    assert answer.degraded is True
    assert [result.title_id for result in answer.results] == [_QUIET]
    assert index.requests[0].mode is SearchMode.FULL_TEXT
    assert index.requests[0].query_vector is None


async def test_an_undegraded_search_does_not_claim_to_be_degraded() -> None:
    """The mirror of the case above, and without it `degraded` could be `True`
    unconditionally -- a warning printed on every search, which an operator
    learns to ignore inside a week."""
    index = _ScriptedIndex(SearchOutcome(hits=(SearchHit(title_id=_QUIET, score=1.0),)))
    service = await _service(index)
    answer = await service.search("vacuum")
    assert answer.requested_mode is SearchMode.FULL_TEXT
    assert answer.mode is SearchMode.FULL_TEXT
    assert answer.degraded is False


async def test_semantic_coverage_is_passed_through_rather_than_recomputed() -> None:
    """Point 3 of "the one thing this milestone must not get wrong". Fails: a
    service computing coverage from its own hits, which reads 1.0 whenever
    every returned hit had a vector -- precisely the case a green test seeds --
    while the real question is what fraction of the *filtered population* had
    one. The two agree exactly in the easy case."""
    index = _ScriptedIndex(
        SearchOutcome(hits=(SearchHit(title_id=_QUIET, score=1.0),), semantic_coverage=0.25)
    )
    service = await _service(index)
    answer = await service.search("vacuum", mode=SearchMode.FULL_TEXT)
    assert answer.semantic_coverage == 0.25


# --- ranking ---------------------------------------------------------------


async def test_an_unowned_match_still_appears_and_still_outranks_an_owned_one() -> None:
    """**PRD 05's "boosted but not exclusive", and the wrong implementation is
    an attractive one.** A ranking that filters to owned returns nothing
    incorrect -- every row on the screen is right, the catalog is just quietly
    smaller. The unowned title is the *stronger* match (index rank 0), so this
    fails a filter **and** a boost big enough to invert the top of the list.
    Position, not membership: `_UNOWNED in ids` passes against an
    implementation that returns the whole table in physical order.
    """
    index = _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_UNOWNED, score=_STRONG),
                SearchHit(title_id=_OWNED, score=_WEAK),
            )
        )
    )
    service = await _service(index, owned=frozenset({_OWNED}))
    answer = await service.search("vacuum")
    assert [result.title_id for result in answer.results] == [_UNOWNED, _OWNED]
    assert [result.owned for result in answer.results] == [False, True]


async def test_an_owned_title_outranks_an_unowned_one_at_equal_relevance() -> None:
    """The mirror, and without it a boost of **zero** passes the case above --
    a term declared in the weight table and never applied to anything.

    Equal *scores* from the index, so both hits take the same dense rank and
    the relevance term cancels exactly. That is why the relevance term groups
    ties rather than using raw position: with a strict positional rank no two
    candidates ever tie, and this property would be unassertable.

    `_UNOWNED < _OWNED` as ids, so a boost of zero ties the two and the
    tiebreak puts the *unowned* one first -- which is what makes the mutation
    visible rather than a coin flip.
    """
    index = _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_UNOWNED, score=_STRONG),
                SearchHit(title_id=_OWNED, score=_STRONG),
            )
        )
    )
    service = await _service(index, owned=frozenset({_OWNED}))
    answer = await service.search("vacuum")
    assert [result.title_id for result in answer.results] == [_OWNED, _UNOWNED]


async def test_a_strong_match_is_not_displaced_by_a_popular_weak_one() -> None:
    """**The incompatible-scale failure, in application code.** Fails: a blend
    adding `hit.score` raw (a `ts_rank` around 0.06) to a popularity term in
    [0, 1), and equally a relevance term spelled `1 / (search_rrf_k + rank)` --
    at k = 60 the whole relevance term is nearly flat while popularity spans
    the unit interval, so the list is popularity-ordered wearing RRF's
    respectable name. ADR-0002 forbids exactly this one layer down; nothing
    catches it one layer up except this case.
    """
    index = _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_QUIET, score=_STRONG),
                SearchHit(title_id=_POPULAR, score=_WEAK),
            )
        )
    )
    service = await _service(index)
    answer = await service.search("vacuum")
    assert answer.results[0].title_id == _QUIET


async def test_an_unknown_popularity_is_not_a_popularity_of_zero() -> None:
    """ADR-0014 in a fourth place. `titles.popularity` is `None` for every
    title TMDb has never described -- most of 1,271,138 rows. Fails:
    `popularity or 0.0` and its SQL twin `coalesce(popularity, 0)`, which rank
    a title nobody measured identically to one measured as unpopular and bury
    the un-enriched catalog beneath the enriched tier while looking like
    arithmetic. The two hits are at equal relevance and equal ownership, so the
    only thing that can separate them is whether absence was scored or excluded.

    `_ZERO_POP < _NO_POP` as ids, so both wrong implementations -- scoring the
    absence, and renormalising by the full weight sum -- tie the pair and the
    tiebreak puts the measured zero first.
    """
    index = _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_ZERO_POP, score=_STRONG),
                SearchHit(title_id=_NO_POP, score=_STRONG),
            )
        )
    )
    service = await _service(index)
    answer = await service.search("vacuum")
    assert [result.title_id for result in answer.results] == [_NO_POP, _ZERO_POP]


async def test_equal_scores_are_broken_by_id_so_two_searches_agree() -> None:
    """Determinism, which is a pagination property before it is a tidiness
    one. Fails: falling back to whatever order the index returned -- and this
    repository has already measured that `UPDATE ... RETURNING` hands rows back
    in heap order on a small table, so "the order it came in" is not an order.
    """
    hits = (
        SearchHit(title_id=_HIGH_ID, score=_STRONG),
        SearchHit(title_id=_LOW_ID, score=_STRONG),
    )
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)))
    answer = await service.search("vacuum")
    assert [result.title_id for result in answer.results] == [_LOW_ID, _HIGH_ID]


async def test_a_hit_whose_title_row_is_gone_is_dropped_rather_than_raising() -> None:
    """A title deleted between the index write and the read. Fails:
    `by_id[hit.title_id]`, which is a `KeyError` -- a 500 on a search because
    one row was removed. The surviving hit is asserted too, so "drop
    everything" is not a pass.
    """
    index = _ScriptedIndex(
        SearchOutcome(
            hits=(
                SearchHit(title_id=_DELETED, score=_STRONG),
                SearchHit(title_id=_QUIET, score=_WEAK),
            )
        )
    )
    service = await _service(index)
    answer = await service.search("vacuum")
    assert [result.title_id for result in answer.results] == [_QUIET]


async def test_the_limit_is_clamped_to_the_configured_ceiling() -> None:
    """`search_result_limit` is the most a caller may ask for, not the
    default. Fails: passing `request.limit` straight through, which makes
    `--limit 10000` a scan wearing a search's name -- every candidate is a row
    hydrated into a `SearchResult` in application code.
    """
    index = _ScriptedIndex(SearchOutcome())
    service = await _service(index, result_limit=20)
    await service.search("vacuum", limit=10_000)
    assert index.requests[0].limit == 20


async def test_a_limit_below_the_ceiling_is_honoured() -> None:
    """The other side of the clamp, without which `min` could be `max` and
    every search would ask the index for the ceiling however small a page the
    caller wanted."""
    index = _ScriptedIndex(SearchOutcome())
    service = await _service(index, result_limit=20)
    await service.search("vacuum", limit=3)
    assert index.requests[0].limit == 3


async def test_the_filters_reach_the_index_unchanged() -> None:
    """The service ranks; it does not narrow. A filter dropped here returns
    *more* rows than were asked for, which reads as working -- the same failure
    `FilterNotSupported` exists to prevent one layer down."""
    index = _ScriptedIndex(SearchOutcome())
    filters = SearchFilters(kinds=(TitleKind.SERIES,), year_from=1999)
    service = await _service(index)
    await service.search("vacuum", filters=filters)
    assert index.requests[0].filters == filters


async def test_suggest_hydrates_and_does_not_re_rank() -> None:
    """`PostgresSuggestIndex` already ordered by edit distance and then by
    popularity *inside* the capped candidate set. Fails: a service applying
    the search blend here, which is popularity counted twice -- once inside the
    cap and once outside it -- and which reorders a type-ahead list away from
    the ordering the narrow path exists to produce.

    The two hits carry the **same** score, so the blend would fall through to
    popularity and put `_POPULAR` first; the suggest index put `_ZERO_POP`
    first and that order is the answer.
    """
    suggestions = _ScriptedSuggest(
        (SearchHit(title_id=_ZERO_POP, score=1.0), SearchHit(title_id=_POPULAR, score=1.0))
    )
    service = await _service(_ScriptedIndex(SearchOutcome()), suggestions=suggestions)
    results = await service.suggest("vac")
    assert [result.title_id for result in results] == [_ZERO_POP, _POPULAR]
    assert [result.name for result in results] == [_CATALOG[_ZERO_POP][0], _CATALOG[_POPULAR][0]]


async def test_suggest_marks_an_owned_candidate() -> None:
    """PRD 05 wants unowned results surfaced "clearly marked", and a type-ahead
    row is a result. Fails: a `suggest` that hydrates the title and leaves
    `owned` at its default, so the badge is absent from the one surface a
    client renders most often."""
    suggestions = _ScriptedSuggest((SearchHit(title_id=_OWNED, score=1.0),))
    service = await _service(
        _ScriptedIndex(SearchOutcome()), suggestions=suggestions, owned=frozenset({_OWNED})
    )
    assert [result.owned for result in await service.suggest("vac")] == [True]


async def test_suggest_clamps_its_limit_too() -> None:
    """The same ceiling, on the path a keystroke drives. Fails: an unclamped
    `suggest`, where the cost of a wrong number is paid on every keypress."""
    suggestions = _ScriptedSuggest()
    service = await _service(
        _ScriptedIndex(SearchOutcome()), suggestions=suggestions, result_limit=20
    )
    await service.suggest("vac", limit=10_000)
    assert suggestions.calls == [("vac", 20)]
