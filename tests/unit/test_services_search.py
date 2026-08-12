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

import ast
import dataclasses
import math
import pathlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.embedding import FakeEmbedder, planted_pair
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository, stored_taste
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.errors import PortUnavailable
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.repository import StoredTaste, TitleEmbeddingUpsert
from usher.ports.rows import RowContext
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
from usher.services.query_expansion import QUERY_KEY, QueryExpansionService
from usher.services.search import (
    SearchService,
    SemanticSearchUnavailable,
    SuggestTier,
    _blend,
)

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
# `_UNPLAYED < _PLAYED` and `_OLD < _UNDATED`, on the same rule as the four
# pairs above: a term whose weight went to zero ties the two rows, and the
# tiebreak then puts the *wrong* one first rather than leaving the order to
# whichever row the fixture happened to seed second.
_UNPLAYED = uuid.UUID(int=0x0A)
_PLAYED = uuid.UUID(int=0x0B)
_OLD = uuid.UUID(int=0x0C)
_UNDATED = uuid.UUID(int=0x0D)
# `_FAR < _NEAR` on the same rule again: the taste term is the smallest weight
# in the table, so an implementation that drops it ties the two rows exactly
# and the tiebreak then puts the *far* one first.
_FAR = uuid.UUID(int=0x0E)
_NEAR = uuid.UUID(int=0x0F)
_SOURCE = uuid.UUID(int=0xFF)
_HOUSEHOLD = uuid.UUID(int=0xA1)
_OTHER_HOUSEHOLD = uuid.UUID(int=0xA2)

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

# The instant the recency term is measured against, injected rather than read
# off the wall clock. A case that asserted an age against `datetime.now(UTC)`
# would assert something slightly different every day and something quite
# different in five years -- and the ordering it is really about (an undated
# title against a dated old one) would go on passing while the arithmetic
# under it drifted.
_NOW = datetime(2026, 8, 11, tzinfo=UTC)

_CATALOG: dict[uuid.UUID, tuple[str, float | None]] = {
    _QUIET: ("The Quiet Vacuum", 0.5),
    _POPULAR: ("Vacuum Sales Quarterly", 1000.0),
    _UNOWNED: ("A Vacuum in Winter", None),
    _OWNED: ("Vacuum Chamber Diaries", None),
    _ZERO_POP: ("Vacuum, Measured", 0.0),
    _NO_POP: ("Vacuum, Undescribed", None),
    _LOW_ID: ("Vacuum Alpha", None),
    _HIGH_ID: ("Vacuum Omega", None),
    _UNPLAYED: ("Vacuum, Unwatched", None),
    _PLAYED: ("Vacuum, Finished", None),
    _OLD: ("Vacuum Antique", None),
    _UNDATED: ("Vacuum, Undated", None),
    _FAR: ("Vacuum, Unlike Yours", None),
    _NEAR: ("Vacuum, Like Yours", None),
}

# The model the household's stored centroid and the stored vectors were both
# written under. A *second* name is what the cross-model case varies, because
# comparing a centroid computed under one checkpoint against vectors stored
# under another is the ST<->fastembed divergence -- max pairwise-similarity
# delta 1.41e-03, 6x the halfvec quantisation error -- arriving as a confident
# cosine rather than as an error.
_TASTE_MODEL = "fake:test-384"
_OTHER_MODEL = "fake:other-checkpoint-384"

# Release years, defaulting to the 2019 every case above was written against.
# Only the recency cases vary it, and `_UNDATED` is the one with **no** year:
# `Title.year` is nullable across the whole catalog, so an absent year is the
# ordinary state of a row rather than a corner.
_DEFAULT_YEAR = 2019
_YEARS: dict[uuid.UUID, int | None] = {_OLD: 1970, _UNDATED: None}


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
        year=_YEARS.get(title_id, _DEFAULT_YEAR),
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


def _finished(title_id: uuid.UUID, *, user_id: uuid.UUID) -> WatchStateMerge:
    """One `played` watch state for a title, not for one of its episodes.

    `played=True` rather than merely "a row exists": a sync writes a row per
    item it observed, so "has a state" is the owned library, and a fixture
    built that way would make the played term agree with the owned one on
    every case in this file.
    """
    return WatchStateMerge(
        user_id=user_id,
        title_id=title_id,
        episode_id=None,
        position_seconds=0,
        played=True,
        runtime_seconds=7200,
        observed_at=_SEEN_AT,
        play_count=1,
        last_played_at=_SEEN_AT,
    )


class _Expander:
    """A real `QueryExpansionService` over a scripted client, plus the two
    things a case asserts on: how many completions were bought, and what landed
    in the ledger.

    Deliberately **not** a stubbed expander. The property under test is *how
    many completions one search buys*, and a stub that recorded a call would
    make every path look identical to the one the composition root builds while
    proving nothing about it.
    """

    def __init__(self, *bodies: dict[str, Any] | BaseException) -> None:
        self.client = FakeLLMClient.returning(*bodies)
        self.ledger = FakeLLMCallRepository()
        self.commits = 0
        self.service = QueryExpansionService(
            client=self.client,
            ledger=self.ledger,
            commit=self._commit,
            model="test/asked-1",
        )

    async def _commit(self) -> None:
        self.commits += 1


class _CountingTitles(FakeTitleRepository):
    """`FakeTitleRepository`, counting the one read `_rank` makes of it."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        self.reads += 1
        return await super().list_by_ids(title_ids)


class _CountingMediaItems(FakeMediaItemRepository):
    """The same, for the ownership read. `FakeMediaItemRepository.calls` exists
    already and does **not** cover `owned_title_ids`, so counting through it
    would be a count of writes."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        self.reads += 1
        return await super().owned_title_ids(title_ids)


class _CountingWatchStates(FakeWatchStateRepository):
    """The same, for the household read this task adds.

    Counted rather than merely observed for its answer: *"exactly one read per
    ranked search, and none at all without a household"* is a property no
    assertion about the returned order can carry -- a service that asked the
    port once per hit would answer identically and cost a statement a hit.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.asked: list[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = []

    async def played_title_ids(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        self.reads += 1
        self.asked.append((user_id, tuple(title_ids)))
        return await super().played_title_ids(user_id, title_ids)


class _CountingTaste(FakeTasteRepository):
    """The stored-centroid probe, counted.

    **One indexed single-row read per ranked search with a household, and none
    at all without one** -- which is the whole cost of the taste term on a
    household that has no stored centroid, and is a number no assertion about
    the returned order can carry.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.asked: list[uuid.UUID] = []

    async def latest(self, user_id: uuid.UUID) -> StoredTaste | None:
        self.reads += 1
        self.asked.append(user_id)
        return await super().latest(user_id)


class _CountingEmbeddings(FakeTitleEmbeddingRepository):
    """The vector read, counted -- and the `model_name` it was scoped by
    recorded beside it.

    The scope is the half a count cannot see: an unscoped read answers with a
    vector from *some* checkpoint and the blend then reports a confident cosine
    between two spaces, which is a plausible number and not an error.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.asked: list[tuple[tuple[uuid.UUID, ...], str | None]] = []

    async def list_for_titles(
        self, title_ids: Sequence[uuid.UUID], *, model_name: str | None = None
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        self.reads += 1
        self.asked.append((tuple(title_ids), model_name))
        return await super().list_for_titles(title_ids, model_name=model_name)


@dataclass(slots=True)
class _Ports:
    """The five repositories `_rank` reads, so a case can count them.

    Held together rather than passed one at a time because the acceptance is
    about the *set* of reads one search makes, and a case that could only see
    one of the five would report four reads as five or five as four.
    """

    titles: _CountingTitles = field(default_factory=_CountingTitles)
    media_items: _CountingMediaItems = field(default_factory=_CountingMediaItems)
    watch_states: _CountingWatchStates = field(default_factory=_CountingWatchStates)
    taste: _CountingTaste = field(default_factory=_CountingTaste)
    embeddings: _CountingEmbeddings = field(default_factory=_CountingEmbeddings)

    @property
    def reads(self) -> int:
        return (
            self.titles.reads
            + self.media_items.reads
            + self.watch_states.reads
            + self.taste.reads
            + self.embeddings.reads
        )


def _cos(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine over two planted unit vectors, for a case's own premise.

    Reads the vectors a case seeded **back through the ports** rather than
    recomputing from the module-level literals it passed in: a premise computed
    from a literal is a premise no fixture change can falsify, which
    `.claude/rules/testing-discipline.md` records four instances of one file
    over.
    """
    return sum(one * other for one, other in zip(left, right, strict=True))


async def _service(
    index: SearchIndex,
    *,
    embedder: FakeEmbedder | None = None,
    owned: frozenset[uuid.UUID] = frozenset(),
    played: frozenset[uuid.UUID] = frozenset(),
    played_by: uuid.UUID = _HOUSEHOLD,
    suggestions: SuggestIndex | None = None,
    # Which tier the scripted suggest index is bound to. The other tier gets an
    # inert one, so a case parametrised over both asks the identical question
    # of each and a service that consulted the wrong collaborator answers
    # nothing.
    tier: SuggestTier = SuggestTier.FUZZY,
    result_limit: int = 50,
    expander: _Expander | None = None,
    ports: _Ports | None = None,
    now: datetime | None = None,
    centroid: Sequence[float] | None = None,
    centroid_for: uuid.UUID = _HOUSEHOLD,
    centroid_model: str = _TASTE_MODEL,
    vectors: Mapping[uuid.UUID, Sequence[float]] | None = None,
    vector_model: str = _TASTE_MODEL,
) -> SearchService:
    """The service over fakes, with the whole invented catalog already stored.

    Seeding every title rather than only the ones a case names keeps the
    hydration read honest: an implementation that returned rows the index never
    mentioned would have somewhere to get them from.

    **`now` is fixed rather than read from the wall clock**, because the
    recency term is a function of it: a case pinning an age against
    `datetime.now(UTC)` would say something slightly different every day it
    ran, and something quite different in five years.

    **`centroid` is *stored*, never computed.** There is no embedder on this
    path and there is none on the shipped route either, so a fixture that
    computed one would be arranging a state no request can reach.
    `centroid=None` -- the default, and every case above this task -- is the
    household with nothing stored, which is also the shipped default.
    """
    kit = _Ports() if ports is None else ports
    for title_id in _CATALOG:
        await kit.titles.add(_title(title_id))
    if owned:
        await kit.media_items.upsert_many([_copy(title_id) for title_id in sorted(owned)])
    if played:
        await kit.watch_states.merge_from_source(
            [_finished(title_id, user_id=played_by) for title_id in sorted(played)]
        )
    if centroid is not None:
        await kit.taste.put(
            stored_taste(
                centroid_for,
                centroid=tuple(centroid),
                model_name=centroid_model,
                title_count=12,
            )
        )
    if vectors:
        # Through `upsert_many` rather than the fake's `given` seeder: `given`
        # also mints a `Title` of its own, and every title in this file already
        # exists with the name, year and popularity its case depends on.
        await kit.embeddings.upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=title_id,
                    embedding=tuple(vector),
                    model_name=vector_model,
                    source_fingerprint="0" * 32,
                )
                for title_id, vector in sorted(vectors.items())
            ]
        )
    scripted: SuggestIndex = _ScriptedSuggest() if suggestions is None else suggestions
    idle = _ScriptedSuggest()
    return SearchService(
        index,
        scripted if tier is SuggestTier.PREFIX else idle,
        scripted if tier is SuggestTier.FUZZY else idle,
        kit.titles,
        kit.media_items,
        kit.watch_states,
        kit.taste,
        kit.embeddings,
        result_limit=result_limit,
        embedder=embedder,
        expander=None if expander is None else expander.service,
        now=(lambda: _NOW) if now is None else (lambda: now),
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


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_a_blank_prefix_never_reaches_the_suggest_index(tier: SuggestTier) -> None:
    """The same refusal on the type-ahead path, which is where a search box
    actually sends whitespace. Fails: a `suggest` that forwards it, which on
    the real backend is a trigram probe with an empty needle against every
    name in a 1,271,138-row table -- or, on tier 1, `LIKE '%'` over the same
    1,271,138 rows collected, de-duplicated and sorted.

    Parametrised over both tiers because the guard is written once above the
    tier selection: a spelling that moved it inside one branch would leave the
    other forwarding whitespace, and one arm cannot see that."""
    suggestions = _ScriptedSuggest((SearchHit(title_id=_QUIET, score=1.0),))
    service = await _service(_ScriptedIndex(SearchOutcome()), suggestions=suggestions, tier=tier)
    assert await service.suggest("  ", tier=tier) == ()
    assert suggestions.calls == []


# --- query expansion -------------------------------------------------------
#
# **The cost claim is half of what these cases are for**, so most of them are
# about the searches that buy *no* completion. `usher suggest` is the one that
# would hurt: a client sends it per keystroke, and an expansion there would
# invert this milestone's whole "one completion per unit of work" argument.


async def test_the_expansion_is_what_gets_embedded_and_the_answer_reports_it() -> None:
    """**Both halves of the clause, in one case, because either alone is the
    defect.** Embedding the rewrite without reporting it is a viewer who
    searched for one thing, got results for another, and has nothing to tell a
    good expansion from a bad one -- and neither has an operator reading their
    bug report. Reporting a rewrite that was not embedded is the same lie from
    the other side.

    The distractor is that `query` is *also* a plausible value for
    `expanded_query`: a service that reported the typed string would pass any
    assertion that the field is merely populated.
    """
    embedder = FakeEmbedder()
    index = _ScriptedIndex(SearchOutcome())
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(index, embedder=embedder, expander=expander)

    answer = await service.search("movies about isolation in space", mode=SearchMode.SEMANTIC)

    assert embedder.calls == [["a crew alone in orbit"]]
    assert answer.expanded_query == "a crew alone in orbit"
    assert index.requests[0].query_vector == tuple(
        (await embedder.embed(["a crew alone in orbit"]))[0]
    )


async def test_the_full_text_lane_still_sees_the_words_the_viewer_typed() -> None:
    """**Only the vector is computed from the rewrite.** Under RRF the lexical
    lane goes on matching the viewer's own words while the semantic lane
    matches the paraphrase, which is strictly more signal than either alone.

    Fails: substituting the rewrite into `SearchRequest.query` as well, which
    leaves *no* lane holding the original -- so a rewrite that drifted turns an
    exact-title search into a search for something else, with a `tsquery` full
    of words the viewer never wrote and nothing to notice it.
    """
    index = _ScriptedIndex(SearchOutcome())
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(index, embedder=FakeEmbedder(), expander=expander)

    await service.search("movies about isolation in space", mode=SearchMode.FUSED)

    assert index.requests[0].query == "movies about isolation in space"


async def test_with_no_expander_the_query_is_embedded_as_typed_and_nothing_is_reported() -> None:
    """**The shipped default, byte for byte.** `USHER_LLM_ENABLED` is `false`,
    so `composition.llm_client` answers `(None, no-op)`, no expander is built,
    and this path has to be exactly M6's. Fails: an `expanded_query` populated
    with the typed string, which would print a line on every search of every
    deployment for a completion nobody bought."""
    embedder = FakeEmbedder()
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=embedder)

    answer = await service.search("movies about isolation in space", mode=SearchMode.SEMANTIC)

    assert embedder.calls == [["movies about isolation in space"]]
    assert answer.expanded_query is None


async def test_an_expansion_that_produced_nothing_leaves_the_query_as_typed() -> None:
    """PRD 08's degradation rule reaching the caller: the endpoint is down, the
    attempt is billed, and the search is served on the words the viewer typed.

    Fails twice over -- a `SearchService` that let the `UsherPortError` out
    would fail a search over an optional enhancement, and one that reported an
    `expanded_query` here would name a rewrite that does not exist.
    """
    embedder = FakeEmbedder()
    expander = _Expander(PortUnavailable("the endpoint refused the connection"))
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=embedder, expander=expander)

    answer = await service.search("movies about isolation in space", mode=SearchMode.SEMANTIC)

    assert embedder.calls == [["movies about isolation in space"]]
    assert answer.expanded_query is None
    assert len(expander.ledger.calls) == 1, "an attempted call is a billed call"


async def test_one_search_buys_exactly_one_completion() -> None:
    """`FakeLLMClient` repeats its last scripted response forever, so a second
    call is invisible to every assertion about *what* came back. One completion
    per unit of work is the milestone's cost argument and the count is the only
    thing that states it."""
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(
        _ScriptedIndex(SearchOutcome()), embedder=FakeEmbedder(), expander=expander
    )

    await service.search("movies about isolation in space", mode=SearchMode.FUSED)

    assert len(expander.client.calls) == 1
    assert len(expander.ledger.calls) == 1


async def test_a_full_text_search_buys_no_completion() -> None:
    """The call sits in front of the *embed*, so a lane with no embed has no
    call in front of it. Fails: an expansion at the top of `search`, which
    bills every `--mode full-text` run -- the mode a deployment with no
    embedding extra uses for everything."""
    embedder = FakeEmbedder()
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=embedder, expander=expander)

    answer = await service.search("vacuum", mode=SearchMode.FULL_TEXT)

    assert expander.client.calls == []
    assert embedder.calls == []
    assert answer.expanded_query is None


async def test_a_blank_query_buys_no_completion() -> None:
    """The blank-query refusal is *before* the model and therefore before this.
    A search box sends one between every keystroke, so an expansion above that
    guard is a completion per keypress -- the exact inverse of this milestone's
    cost argument, arriving on its most frequent path."""
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(
        _ScriptedIndex(SearchOutcome()), embedder=FakeEmbedder(), expander=expander
    )

    answer = await service.search("   ", mode=SearchMode.SEMANTIC)

    assert expander.client.calls == []
    assert answer.expanded_query is None


async def test_a_fused_search_with_no_embedder_buys_no_completion() -> None:
    """Nothing is going to be embedded, so there is nothing to expand *for*.
    Fails: an expansion in front of the `embedder is None` branch, which buys a
    rewrite and then throws it away -- billed, on every fused search of a
    deployment that has no model at all, which is the shipped default."""
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    index = _ScriptedIndex(SearchOutcome(hits=(SearchHit(title_id=_QUIET, score=1.0),)))
    service = await _service(index, embedder=None, expander=expander)

    answer = await service.search("movies about isolation in space", mode=SearchMode.FUSED)

    assert expander.client.calls == []
    assert answer.degraded is True
    assert answer.expanded_query is None


async def test_a_semantic_search_with_no_embedder_buys_no_completion() -> None:
    """The other arm of the same branch. `SemanticSearchUnavailable` is raised
    before anything is spent, because a deployment configured without a model
    has not failed -- it said so once, at startup -- and charging it for the
    sentence would be spend an operator has to explain away."""
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(_ScriptedIndex(SearchOutcome()), embedder=None, expander=expander)

    with pytest.raises(SemanticSearchUnavailable):
        await service.search("movies about isolation in space", mode=SearchMode.SEMANTIC)

    assert expander.client.calls == []
    assert expander.ledger.calls == []


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_type_ahead_buys_no_completion(tier: SuggestTier) -> None:
    """**The one that would hurt.** `suggest` is what a client calls per
    keystroke; it has no semantic lane at all, so it has no embed for an
    expansion to sit in front of. Fails: an expansion factored to the top of
    the service and shared by both entry points, which is the tidy-looking
    version and is a completion per keypress."""
    suggestions = _ScriptedSuggest((SearchHit(title_id=_QUIET, score=1.0),))
    expander = _Expander({QUERY_KEY: "a crew alone in orbit"})
    service = await _service(
        _ScriptedIndex(SearchOutcome()),
        embedder=FakeEmbedder(),
        suggestions=suggestions,
        tier=tier,
        expander=expander,
    )

    assert await service.suggest("the quie", tier=tier) != ()
    assert expander.client.calls == []


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


async def test_a_played_title_outranks_an_unplayed_one_at_equal_relevance() -> None:
    """PRD 05's watch-state term, and **the direction is the decision**: played
    is a small boost, never a demotion.

    A search is overwhelmingly a re-find intent -- somebody typing a title's
    name usually wants that title -- so a demotion buries the exact film they
    just named. Fails: no watch-state term at all (the two rows tie and the
    tiebreak puts `_UNPLAYED` first, because `_UNPLAYED < _PLAYED`), a term
    whose weight is zero, and a term with the sign the other way round.

    **Its premise is asserted first**: the two hits carry *equal index scores*,
    so `_dense_ranks` gives them one rank and the relevance term cancels
    exactly. Under a strict positional rank no two candidates ever tie and this
    property would be unassertable -- which is the trap `_dense_ranks`'
    docstring already records by name for the owned boost.
    """
    hits = (
        SearchHit(title_id=_UNPLAYED, score=_STRONG),
        SearchHit(title_id=_PLAYED, score=_STRONG),
    )
    assert {hit.score for hit in hits} == {_STRONG}, (
        "the premise: equal index scores, so the relevance term cancels and the "
        "watch-state term is the only thing left that can separate the two"
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)), played=frozenset({_PLAYED}), ports=ports
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    assert await ports.watch_states.played_title_ids(_HOUSEHOLD, [_PLAYED]) == {_PLAYED}, (
        "the premise: the household really has finished this one"
    )
    assert [result.title_id for result in answer.results] == [_PLAYED, _UNPLAYED]


async def test_two_households_that_disagree_about_one_title_get_different_orders() -> None:
    """The point of the parameter, and the trap it invites.

    Two households, one query, one candidate set -- and they must come back in
    *different* orders, because they disagree about the one signal this task
    adds. Fails: a `user_id` accepted and dropped, and a `played_title_ids`
    read whose `user_id` argument is ignored (the fake's own scope is asserted
    by its contract; what this pins is that the service passes the household it
    was given).

    **A case that seeded two households which happened to agree would pass
    against every one of those**, which is why the premise here is the
    disagreement itself rather than the two answers.
    """
    hits = (
        SearchHit(title_id=_UNPLAYED, score=_STRONG),
        SearchHit(title_id=_PLAYED, score=_STRONG),
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)), played=frozenset({_PLAYED}), ports=ports
    )
    candidates = [_UNPLAYED, _PLAYED]
    mine = await ports.watch_states.played_title_ids(_HOUSEHOLD, candidates)
    theirs = await ports.watch_states.played_title_ids(_OTHER_HOUSEHOLD, candidates)
    assert mine != theirs, (
        "the premise: the two households genuinely differ on the signal under "
        f"test -- both answered {mine}, so any difference in the two orders "
        "below would be coming from somewhere else"
    )

    ours = await service.search("vacuum", user_id=_HOUSEHOLD)
    yours = await service.search("vacuum", user_id=_OTHER_HOUSEHOLD)

    assert [result.title_id for result in ours.results] == [_PLAYED, _UNPLAYED]
    assert [result.title_id for result in yours.results] == [_UNPLAYED, _PLAYED]


async def test_a_ranked_search_for_a_household_with_no_centroid_makes_exactly_four_reads() -> None:
    """`list_by_ids`, `owned_title_ids`, `played_title_ids`, `latest` -- one
    each, whatever the hit count, and **`list_for_titles` not at all**.

    This is the shipped default and therefore the number that matters: no
    worker has run, `user_taste` is empty, and the taste term costs exactly one
    indexed single-row probe. Fails: a per-hit `played_title_ids`, which is the
    N+1 the batch read exists to delete; and a vector read issued before the
    centroid is known to exist, which is a `WHERE title_id IN (...)` over the
    whole candidate set on every search of every deployment that has never
    indexed anything -- answering `{}`, so no assertion about the order can see
    it.
    """
    hits = tuple(SearchHit(title_id=title_id, score=_STRONG) for title_id in sorted(_CATALOG)[:6])
    assert len(hits) == 6, "the premise: more hits than reads, or a count proves nothing"
    ports = _Ports()
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)), ports=ports)

    await service.search("vacuum", user_id=_HOUSEHOLD)

    # Captured before the premise is checked, because checking it is itself a
    # `latest` and would otherwise be counted as one of the search's own.
    counts = (
        ports.titles.reads,
        ports.media_items.reads,
        ports.watch_states.reads,
        ports.taste.reads,
        ports.embeddings.reads,
    )
    assert await ports.taste.latest(_HOUSEHOLD) is None, (
        "the premise: this household has nothing stored, so the vector read is "
        "the one that must not happen"
    )
    assert counts == (1, 1, 1, 1, 0)
    assert sum(counts) == 4


async def test_a_ranked_search_for_a_household_with_a_centroid_makes_exactly_five() -> None:
    """The other arm, and the fifth read is the vector one.

    Fails: a `list_for_titles` issued per hit rather than per search -- which
    answers identically and costs a statement a hit -- and a `latest` re-read
    once per candidate, which is the same defect on the cheaper port.
    """
    axis, near_vector = planted_pair(math.pi / 3)
    hits = tuple(SearchHit(title_id=title_id, score=_STRONG) for title_id in sorted(_CATALOG)[:6])
    assert len(hits) == 6, "the premise: more hits than reads, or a count proves nothing"
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        vectors={_NEAR: near_vector},
        ports=ports,
    )

    await service.search("vacuum", user_id=_HOUSEHOLD)

    assert (
        ports.titles.reads,
        ports.media_items.reads,
        ports.watch_states.reads,
        ports.taste.reads,
        ports.embeddings.reads,
    ) == (1, 1, 1, 1, 1)
    assert ports.reads == 5


async def test_a_stored_refusal_costs_the_probe_and_not_the_vector_read() -> None:
    """A household below `TasteService._MIN_TITLES` has a **written refusal** —
    a `user_taste` row whose `centroid` is NULL — and that is a readable row
    rather than an absence.

    Fails: `if stored is not None` as the gate on the vector read, which is the
    obvious spelling and which pays a `WHERE title_id IN (...)` over every
    candidate for a household there is provably nothing to compare against; and
    an implementation that raised on the refusal, which is a 500 on a search
    for the emptiest household in the deployment.
    """
    hits = tuple(SearchHit(title_id=title_id, score=_STRONG) for title_id in sorted(_CATALOG)[:6])
    ports = _Ports()
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)), ports=ports)
    await ports.taste.put(
        stored_taste(_HOUSEHOLD, centroid=None, model_name=_TASTE_MODEL, title_count=3)
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    stored = await ports.taste.latest(_HOUSEHOLD)
    assert stored is not None and stored.centroid is None, (
        "the premise: the row is present and its centroid is the written refusal"
    )
    assert len(answer.results) == 6
    assert ports.embeddings.reads == 0


async def test_a_ranked_search_with_no_household_makes_exactly_two() -> None:
    """The other side, and it is not tidiness: `played_title_ids` and `latest`
    both need a `user_id`, so a service that asked anyway would have to invent
    one.

    Fails: a household read issued with a placeholder id, which costs two
    statements per search on every caller that has no household and answers
    about a user nobody is.
    """
    hits = tuple(SearchHit(title_id=title_id, score=_STRONG) for title_id in sorted(_CATALOG)[:6])
    ports = _Ports()
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)), ports=ports)

    await service.search("vacuum")

    assert (
        ports.titles.reads,
        ports.media_items.reads,
        ports.watch_states.reads,
        ports.taste.reads,
        ports.embeddings.reads,
    ) == (1, 1, 0, 0, 0)
    assert ports.reads == 2


async def test_the_household_read_is_bounded_by_the_hits() -> None:
    """`played_title_ids` is asked about the candidates and nothing else.

    Fails: a read of the household's whole history, which is unbounded in the
    one dimension a search cannot bound -- and which answers correctly, so only
    the argument says which was written.
    """
    hits = (SearchHit(title_id=_PLAYED, score=_STRONG),)
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        played=frozenset({_PLAYED, _UNPLAYED}),
        ports=ports,
    )

    await service.search("vacuum", user_id=_HOUSEHOLD)

    assert ports.watch_states.asked == [(_HOUSEHOLD, (_PLAYED,))]


async def test_a_search_that_matched_nothing_asks_no_household_anything() -> None:
    """The empty-candidate guard, on the read this task adds. Fails: a `_rank`
    that reads before it checks, which is a statement per keystroke on a search
    box whose query has not matched yet -- most keystrokes."""
    ports = _Ports()
    service = await _service(_ScriptedIndex(SearchOutcome()), ports=ports)

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    assert answer.results == ()
    assert ports.reads == 0


async def test_an_undated_title_outranks_a_measured_old_one_at_equal_relevance() -> None:
    """ADR-0014 in a fifth place, after `_popularity_term`'s fourth.

    `Title.year` is null across most of a bootstrap catalog, and
    `year or 0` -- or any spelling that scores the absence -- would put every
    undated row at maximum age and bury the un-enriched catalog beneath the
    enriched tier while looking like arithmetic. Fails that, and fails a
    `_blend` that renormalised by the full weight sum instead of by the present
    one.

    `_OLD < _UNDATED` as ids, so both wrong implementations tie or invert the
    pair and the tiebreak puts the measured old one first.
    """
    hits = (
        SearchHit(title_id=_OLD, score=_STRONG),
        SearchHit(title_id=_UNDATED, score=_STRONG),
    )
    assert _YEARS[_OLD] is not None and _YEARS[_UNDATED] is None, (
        "the premise: one row carries a measured year and the other carries none"
    )
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)))

    answer = await service.search("vacuum")

    assert [result.title_id for result in answer.results] == [_UNDATED, _OLD]


async def test_a_newer_title_outranks_an_older_one_at_equal_relevance() -> None:
    """The other half, without which a recency term of **zero** passes the case
    above -- absence would still beat a measured zero and nothing would say the
    term does any work between two dated rows.

    Both rows are dated, unowned and unmeasured for popularity, so recency is
    the only signal that can separate them.
    """
    hits = (
        SearchHit(title_id=_OLD, score=_STRONG),
        SearchHit(title_id=_LOW_ID, score=_STRONG),
    )
    assert _YEARS[_OLD] < _YEARS.get(_LOW_ID, _DEFAULT_YEAR), (  # type: ignore[operator]
        "the premise: the second row really is the newer one"
    )
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)))

    answer = await service.search("vacuum")

    assert [result.title_id for result in answer.results] == [_LOW_ID, _OLD]


async def test_a_title_near_the_household_centroid_outranks_a_far_one_at_equal_relevance() -> None:
    """PRD 05's sixth ranking term, and **the angle is planted rather than
    hoped for out of the hashing fake**.

    `FakeEmbedder` is `blake2b -> Box-Muller -> L2-normalise`, whose measured
    off-diagonal cosine is mean -0.00001 / sd 0.05102 with **zero pairs above
    0.5** -- so "these two titles are similar" is not a thing a hash can be
    asked for, and a case built on one asserts nothing about the term.
    `planted_pair` gives `dot(a, cos(t)*a + sin(t)*b) == cos(t)` exactly, to
    2.22e-16.

    Fails: no taste term at all (the two rows tie exactly and the tiebreak puts
    `_FAR` first, because `_FAR < _NEAR`), a term whose weight is zero, a term
    read off `TasteService.centroid` (which is structurally `None` on any
    process holding no embedder, so it would tie too), and a term with the sign
    the other way round.

    **Its premise is asserted first and read back through the ports**, not
    recomputed from the literals the fixture was handed: equal index scores, so
    `_dense_ranks` gives the two hits one rank and the relevance term cancels
    exactly; and the stored centroid really is nearer the one row than the
    other.
    """
    axis, near_vector = planted_pair(math.pi / 3)
    _, far_vector = planted_pair(math.pi / 2)
    hits = (
        SearchHit(title_id=_FAR, score=_STRONG),
        SearchHit(title_id=_NEAR, score=_STRONG),
    )
    assert {hit.score for hit in hits} == {_STRONG}, (
        "the premise: equal index scores, so the relevance term cancels and the "
        "taste term is the only thing left that can separate the two"
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        vectors={_NEAR: near_vector, _FAR: far_vector},
        ports=ports,
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    stored = await ports.taste.latest(_HOUSEHOLD)
    assert stored is not None and stored.centroid is not None
    seeded = await ports.embeddings.list_for_titles([_NEAR, _FAR], model_name=stored.model_name)
    assert _cos(stored.centroid, seeded[_NEAR]) > _cos(stored.centroid, seeded[_FAR]), (
        "the premise: the stored centroid really is nearer one of the two -- "
        f"near {_cos(stored.centroid, seeded[_NEAR])}, far "
        f"{_cos(stored.centroid, seeded[_FAR])}"
    )
    assert [result.title_id for result in answer.results] == [_NEAR, _FAR]


async def test_a_centroid_from_one_model_never_ranks_a_vector_stored_under_another() -> None:
    """The failure that produces a **plausible number** rather than an error.

    `title_embeddings` is not scoped to a checkpoint — a deployment mid-swap
    holds two — and the measured ST-vs-fastembed difference is a max pairwise
    similarity delta of 1.41e-03, **6x the halfvec quantisation error**. So a
    cosine taken across the two is not slightly worse; it is a confident
    statement about two different spaces, and it raises nothing.

    Both stored vectors here are under the *other* model, so the correct answer
    is that neither has a term: the two rows tie and the tiebreak orders them.
    An unscoped read would find both, and the two vectors are deliberately at
    **different** angles from the centroid, so it would order them the other
    way round. The `model_name` that crossed the port is asserted as well,
    because the outcome alone is also what "no taste term at all" produces.
    """
    axis, near_vector = planted_pair(math.pi / 3)
    _, far_vector = planted_pair(math.pi / 2)
    hits = (
        SearchHit(title_id=_FAR, score=_STRONG),
        SearchHit(title_id=_NEAR, score=_STRONG),
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        centroid_model=_TASTE_MODEL,
        vectors={_NEAR: near_vector, _FAR: far_vector},
        vector_model=_OTHER_MODEL,
        ports=ports,
    )
    assert _cos(axis, near_vector) > _cos(axis, far_vector), (
        "the premise: an unscoped read really would order these two, so the "
        "tie below is the scope doing something rather than the fixture being flat"
    )
    assert _TASTE_MODEL != _OTHER_MODEL, "the premise: two checkpoints, not one"

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    assert ports.embeddings.asked == [((_FAR, _NEAR), _TASTE_MODEL)]
    assert await ports.embeddings.list_for_titles([_NEAR, _FAR]) != {}, (
        "the premise: the vectors really are stored — an empty table would make "
        "the scope unobservable"
    )
    assert [result.title_id for result in answer.results] == [_FAR, _NEAR]


async def test_a_hit_with_no_vector_is_not_a_cosine_of_zero() -> None:
    """ADR-0014 in a sixth place, and here the collapse is uniquely tempting
    because `0.0` is a *reachable* cosine rather than an impossible one.

    A measured zero says "these two are orthogonal", which is a claim about two
    vectors; "the backfill has not reached this title" is a claim about a job
    queue. `_blend` drops an absent signal from numerator **and** denominator,
    so the un-embedded row is scored on what is known about it — and
    `title_embeddings` is empty on every catalog this project currently has, so
    the un-embedded row is the population.

    `_FAR` carries the measured zero and `_NEAR` carries no vector at all, so a
    `taste or 0.0` spelling ties the two and the tiebreak puts `_FAR` first.
    """
    axis, orthogonal = planted_pair(math.pi / 2)
    hits = (
        SearchHit(title_id=_FAR, score=_STRONG),
        SearchHit(title_id=_NEAR, score=_STRONG),
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        vectors={_FAR: orthogonal},
        ports=ports,
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    seeded = await ports.embeddings.list_for_titles([_NEAR, _FAR], model_name=_TASTE_MODEL)
    assert _NEAR not in seeded and _FAR in seeded, (
        "the premise: one row has a vector and the other has none"
    )
    assert _cos(axis, seeded[_FAR]) == pytest.approx(0.0, abs=1e-15), (
        "the premise: the one that does have a vector measures orthogonal, "
        "which is the value the absent one must not be scored as"
    )
    assert [result.title_id for result in answer.results] == [_NEAR, _FAR]


async def test_a_negative_cosine_is_no_affinity_and_not_a_penalty() -> None:
    """The lower clamp, which is the arm that touches real data.

    `_blend` is only a weighted *mean* if every term is in `[0, 1]`; an
    unclamped negative cosine makes the taste weight a **penalty** of unbounded
    relative size on exactly the rows the term knows least about. And "pointing
    away from the centroid" is not a measured statement about dislike — the
    corpus-level distribution that would license reading it that way does not
    exist in this project — so the two rows below are equally *un*-endorsed and
    the term must say nothing about which is worse.

    `_FAR` is at 2π/3 (cosine -0.5) and `_NEAR` at π/2 (cosine 0.0): clamped,
    both terms are 0.0 and the tiebreak orders them; unclamped, `_FAR` is
    pushed below `_NEAR` and the answer inverts.
    """
    axis, orthogonal = planted_pair(math.pi / 2)
    _, opposed = planted_pair(2 * math.pi / 3)
    hits = (
        SearchHit(title_id=_FAR, score=_STRONG),
        SearchHit(title_id=_NEAR, score=_STRONG),
    )
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        vectors={_FAR: opposed, _NEAR: orthogonal},
        ports=ports,
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    seeded = await ports.embeddings.list_for_titles([_NEAR, _FAR], model_name=_TASTE_MODEL)
    assert _cos(axis, seeded[_FAR]) < 0.0 < _cos(axis, seeded[_NEAR]) + 1e-15, (
        "the premise: one cosine really is negative and the other is not, so an "
        f"unclamped term would separate them -- far {_cos(axis, seeded[_FAR])}, "
        f"near {_cos(axis, seeded[_NEAR])}"
    )
    scores = {result.title_id: result.score for result in answer.results}
    assert scores[_FAR] == scores[_NEAR]
    assert [result.title_id for result in answer.results] == [_FAR, _NEAR]


async def test_the_taste_weight_is_pinned_by_arithmetic_rather_than_by_an_ordering() -> None:
    """F4's finding, applied to the weight F4 left room for: **a weight table
    is not pinned by any number of ordering cases.** Re-balancing `owned` from
    0.15 to 0.10 left all ten of this file's ordering cases green and failed
    exactly one assertion, the numeric one.

    `_UNDATED` has no popularity and no year, so with a household exactly four
    signals are present — relevance, owned, played and taste — and `_blend`
    renormalises over those four. The vector is planted at **θ = 0**, which is
    exact in binary at every step (`cos(0.0)` is 1.0 and `sin(0.0)` is 0.0), so
    the taste term is 1.0 to the bit and the expected score is a closed form.

    **Every literal is written out rather than read from `_WEIGHTS`**, for the
    reason the M6 pin below gives: a case whose expectation is derived from the
    constant under test pins that the constant is in force and cannot pin its
    value.
    """
    axis, identical = planted_pair(0.0)
    hits = (SearchHit(title_id=_UNDATED, score=_STRONG),)
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome(hits=hits)),
        centroid=axis,
        vectors={_UNDATED: identical},
        ports=ports,
    )
    assert _YEARS[_UNDATED] is None and _CATALOG[_UNDATED][1] is None, (
        "the premise: no year and no popularity, so those two signals are absent "
        "and the denominator is the four below"
    )

    answer = await service.search("vacuum", user_id=_HOUSEHOLD)

    assert _cos(axis, identical) == 1.0, "the premise: the angle is exact, not approximate"
    assert answer.results[0].score == (
        (0.70 * 1.0 + 0.15 * 0.0 + 0.02 * 0.0 + 0.005 * 1.0) / (0.70 + 0.15 + 0.02 + 0.005)
    )


def test_no_combination_of_the_other_five_can_displace_an_exact_match() -> None:
    """PRD 05's *"boosted but not exclusive"* as arithmetic, restated over six
    signals — and **the reason `taste` is 0.005 rather than the 0.01 of
    headroom the table appeared to leave.**

    A rank-0 hit with every other signal against it against a rank-1 hit with
    every other signal maximally for it. The two present-signal sets are equal,
    so the denominators are equal and the comparison is between numerators:
    `0.70` against `0.35 + 0.15 + 0.15 + 0.02 + 0.02 + w`.

    🔴 At `w = 0.01` that sum is **0.7000000000000001** in IEEE-754 doubles —
    one ulp *above* 0.70 — so the challenger wins and the property fails. Not a
    tie broken by id: an inversion, and one that only a case built at this
    exact configuration can see. That is why the interval is open and why the
    weight is its midpoint.

    Driven through `_blend` directly rather than through a fixture, because
    "popularity maximally for it" is asymptotic (`p / (p + 10)` never reaches
    1.0) and no seeded catalog can reach the corner the bound is about.
    """
    exact = _blend(relevance=1.0, popularity=0.0, owned=0.0, played=0.0, recency=0.0, taste=0.0)
    challenger = _blend(
        relevance=0.5, popularity=1.0, owned=1.0, played=1.0, recency=1.0, taste=1.0
    )

    assert exact > challenger, (
        f"an exact match at {exact} was displaced by a rank-1 hit at {challenger}"
    )
    # The literals, so the bound is pinned to these six numbers and not merely
    # to whatever `_WEIGHTS` currently holds.
    denominator = 0.70 + 0.15 + 0.15 + 0.02 + 0.02 + 0.005
    assert exact == 0.70 / denominator
    assert challenger == (0.35 + 0.15 + 0.15 + 0.02 + 0.02 + 0.005) / denominator
    # And the measurement that closed the interval, asserted rather than
    # described: the value this table's headroom appeared to permit inverts it.
    assert 0.35 + 0.15 + 0.15 + 0.02 + 0.02 + 0.01 > 0.70


async def test_with_no_household_and_no_year_the_score_is_the_one_m6_computed() -> None:
    """The numeric pin, and it is numeric rather than an ordering on purpose: a
    re-weighting that reordered nothing would pass every case above and change
    every score on the wire.

    A hit with no popularity, no year and no household has exactly two present
    signals -- relevance and owned -- and `_blend` renormalises over those two,
    so the answer has to be M6's to the last bit. **Both literals below are
    written out rather than read from `_WEIGHTS`**: a case whose expectation is
    derived from the constant under test pins that the constant is in force and
    cannot pin its value.
    """
    hits = (SearchHit(title_id=_UNDATED, score=_STRONG),)
    service = await _service(_ScriptedIndex(SearchOutcome(hits=hits)))

    answer = await service.search("vacuum")

    assert answer.results[0].score == (0.70 * 1.0 + 0.15 * 0.0) / (0.70 + 0.15)


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


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_suggest_hydrates_and_does_not_re_rank(tier: SuggestTier) -> None:
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
    service = await _service(_ScriptedIndex(SearchOutcome()), suggestions=suggestions, tier=tier)
    results = await service.suggest("vac", tier=tier)
    assert [result.title_id for result in results] == [_ZERO_POP, _POPULAR]
    assert [result.name for result in results] == [_CATALOG[_ZERO_POP][0], _CATALOG[_POPULAR][0]]


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_suggest_marks_an_owned_candidate(tier: SuggestTier) -> None:
    """PRD 05 wants unowned results surfaced "clearly marked", and a type-ahead
    row is a result. Fails: a `suggest` that hydrates the title and leaves
    `owned` at its default, so the badge is absent from the one surface a
    client renders most often."""
    suggestions = _ScriptedSuggest((SearchHit(title_id=_OWNED, score=1.0),))
    service = await _service(
        _ScriptedIndex(SearchOutcome()),
        suggestions=suggestions,
        tier=tier,
        owned=frozenset({_OWNED}),
    )
    assert [result.owned for result in await service.suggest("vac", tier=tier)] == [True]


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_suggest_clamps_its_limit_too(tier: SuggestTier) -> None:
    """The same ceiling, on the path a keystroke drives. Fails: an unclamped
    `suggest`, where the cost of a wrong number is paid on every keypress."""
    suggestions = _ScriptedSuggest()
    service = await _service(
        _ScriptedIndex(SearchOutcome()), suggestions=suggestions, tier=tier, result_limit=20
    )
    await service.suggest("vac", limit=10_000, tier=tier)
    assert suggestions.calls == [("vac", 20)]


@pytest.mark.parametrize("tier", list(SuggestTier))
async def test_suggest_hydrates_with_two_reads_whatever_the_tier_and_whatever_the_hit_count(
    tier: SuggestTier,
) -> None:
    """`list_by_ids` then `owned_title_ids`, one each, from either tier.

    **The count is the N+1 assertion** -- a `suggest` that hydrated per hit
    answers the identical box, and only a count over more hits than reads can
    tell the two apart. Six hits, two reads, on the path a client drives per
    keystroke.

    **It is not the "written once" assertion**, which is what
    `test_the_hydration_is_written_once_rather_than_once_per_tier` is for: a
    body spelled `if tier is PREFIX: <two reads> else: <two reads>` passes
    everything here, on both arms.

    The three reads `_rank` makes for a household are absent by construction --
    `suggest` takes no `user_id`, runs no blend, and therefore reads no watch
    state, no centroid and no vectors. That absence is asserted rather than
    described, because it is the reason a keystroke is cheap.
    """
    hits = tuple(SearchHit(title_id=title_id, score=1.0) for title_id in sorted(_CATALOG)[:6])
    assert len(hits) == 6, "the premise: more hits than reads, or a count proves nothing"
    ports = _Ports()
    service = await _service(
        _ScriptedIndex(SearchOutcome()),
        suggestions=_ScriptedSuggest(hits),
        tier=tier,
        ports=ports,
    )

    results = await service.suggest("vac", limit=10, tier=tier)

    assert len(results) == 6, "the premise: the hydration returned every hit"
    counts = (
        ports.titles.reads,
        ports.media_items.reads,
        ports.watch_states.reads,
        ports.taste.reads,
        ports.embeddings.reads,
    )
    assert counts == (1, 1, 0, 0, 0)


def test_the_hydration_is_written_once_rather_than_once_per_tier() -> None:
    """**Structural, because the behavioural count above cannot see this.**

    Two reads per tier is what a shared body produces *and* what a body
    duplicated inside an `if tier is ...` produces. The two answer identically
    on the day they are written and drift the first time either tier grows a
    field -- an `owned` flag added to one arm, a `list_by_ids` narrowed in the
    other -- and nothing behavioural notices, because each arm is still
    correct about itself.

    So the claim `SearchService.suggest`'s own docstring makes is asserted the
    only way it can be: the two hydration reads appear **once each** in that
    function's body. Same move C4 made for a defect whose only symptom was
    which thread ran.

    Fails: the per-tier duplication, and also a `suggest` that reached for a
    third read.
    """
    source = (_SERVICES / "search.py").read_text()
    tree = ast.parse(source)
    bodies = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "suggest"
    ]
    # The premise: a scan that found no function passes exactly like a scan
    # that found a correct one.
    assert len(bodies) == 1, f"the scan found {len(bodies)} `suggest` definitions"
    called = [
        node.func.attr
        for node in ast.walk(bodies[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert called.count("list_by_ids") == 1, called
    assert called.count("owned_title_ids") == 1, called


# --- the home screen reaches none of this ----------------------------------

_SERVICES = pathlib.Path(__file__).parents[2] / "src" / "usher" / "services"

#: Every name a row provider would have to write down to reach the blend. The
#: module is in the list as well as the symbols, because
#: `import usher.services.search` needs none of them.
_RANKING_NAMES = frozenset(
    {
        "usher.services.search",
        "SearchService",
        "SearchAnswer",
        "_WEIGHTS",
        "_blend",
        "_dense_ranks",
        "_popularity_term",
        "_recency_term",
        "_taste_term",
    }
)


def test_the_home_screen_and_its_providers_reach_no_ranking_term() -> None:
    """The claim that makes `GET /home`'s measured budget cheap to hold.

    Six ranking terms now, five repository reads with a household, and a
    clock -- none of which the home screen pays for, because no row provider
    and no composer can reach any of it. Asserted structurally rather than by
    re-measuring the 5,200-copy household's figures: a timing run proves the
    cost is absent today, and this proves there is no path by which it could
    arrive.

    `RowContext` is the other half. It carries thirteen collaborators and
    **no `search` field**, so a provider that wanted a blended score would have
    to be handed one first.
    """
    modules = [_SERVICES / "home.py", *sorted((_SERVICES / "rows").glob("*.py"))]
    # The premise, because a glob that matched nothing passes exactly like a
    # scan that found nothing to report: ten providers, their base and their
    # registry, plus the composer.
    assert len(modules) >= 12, f"the scan found only {len(modules)} modules: {modules}"

    for path in modules:
        named: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                named.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                named.add(node.module or "")
                named.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Attribute | ast.Name):
                named.add(node.attr if isinstance(node, ast.Attribute) else node.id)
        reachable = named & _RANKING_NAMES
        assert not reachable, f"{path.name} names {sorted(reachable)}"

    assert "search" not in {one.name for one in dataclasses.fields(RowContext)}
