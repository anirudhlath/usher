"""What every `SearchIndex` implementation owes its callers.

Runs against `FakeSearchIndex` (tests/unit/test_search_index_contract.py,
no Docker) and `PostgresSearchIndex` (tests/integration/
test_adapters_search_postgres.py, real Postgres, real `tsquery`, real
`ts_rank`, real HNSW). One copy of the assertions, two implementations --
the pattern PRD 08 names for `SourceAdapter`, applied to the port M6 ships.

**Every case here asserts on position, not membership, with exactly one
named exception.** `assert expected in {hit.title_id for hit in hits}`
passes against an implementation that returns the whole table in physical
order, and so does `assert hits`. Each case seeds a distractor a broken
implementation would rank *first*, and each docstring names the wrong
implementation it fails -- a ranking case that cannot say what it rules out
is a case that gets loosened the first time it goes red.

Subclass and provide an `index` fixture plus the `given_title_row` hook:

    class TestFakeSearchIndex(SearchIndexContract):
        supports_semantic = True
        unsupported_filter = "owned_only"

        @pytest.fixture
        def index(self) -> FakeSearchIndex:
            return FakeSearchIndex()

        async def given_title_row(self, title_id: uuid.UUID) -> None:
            return None
"""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.ports.search import (
    FilterNotSupported,
    SearchDocument,
    SearchFilters,
    SearchIndex,
    SearchMode,
    SearchRequest,
)


def _document(
    name: str,
    *,
    overview: str | None = None,
    genres: tuple[str, ...] = (),
    kind: TitleKind = TitleKind.MOVIE,
    popularity: float = 1.0,
    year: int | None = 2019,
    vector: tuple[float, ...] | None = None,
) -> SearchDocument:
    """One synthetic document. Every name in this file is invented; nothing
    here is a row from any third-party dataset (see
    tests/unit/test_no_third_party_data.py)."""
    return SearchDocument(
        title_id=new_id(),
        kind=kind,
        name=name,
        sort_name=name,
        overview=overview,
        genres=genres,
        popularity=popularity,
        year=year,
        vector=vector,
    )


class SearchIndexContract:
    # Defaults to False so a driver that forgets to declare it *skips*,
    # loudly and visibly in pytest's summary, rather than passing four
    # semantic cases it never ran. Both of today's drivers set it True; the
    # flag exists for a backend with no vector storage configured.
    supports_semantic: bool = False

    # The name of one `SearchFilters` member this implementation genuinely
    # cannot express, or None if it expresses the whole vocabulary. Not a
    # bool: the case has to *build* the filter, and naming the field is what
    # keeps the skip honest about which one.
    unsupported_filter: str | None = None

    async def given_title_row(self, title_id: uuid.UUID) -> None:
        """Arrangement a real index constrains and a dict does not.

        `PostgresSearchIndex` writes into a table whose `title_id` is a
        foreign key onto `titles`, so its driver inserts a row first;
        `FakeSearchIndex` is a dict and needs nothing. A hook rather than a
        `titles` insert in every case, for the reason
        `CredentialStoreContract.owner` is a hook: the constraint belongs to
        one implementation, and writing it into the shared suite would make
        the suite know about Postgres.
        """
        raise NotImplementedError

    async def index_all(self, index: SearchIndex, documents: Sequence[SearchDocument]) -> None:
        """Every arrangement goes through here, so no case can forget the
        hook. A case that indexed directly would pass against the fake and
        fail against Postgres on a foreign key, which is a suite that only
        runs in one place."""
        for document in documents:
            await self.given_title_row(document.title_id)
        await index.index_many(documents)

    # --- retrieval ---------------------------------------------------------

    async def test_an_indexed_document_is_findable_by_its_name(self, index: SearchIndex) -> None:
        """The empty implementation, and nothing else.

        **The only membership assertion this suite permits**, and it is the
        weakest case in it: it passes against an implementation that returns
        every document for every query. That is fine here and nowhere else,
        because its whole job is to fail a `search` that returns `[]`.
        """
        document = _document("The Quiet Vacuum")
        await self.index_all(index, [document])
        outcome = await index.search(SearchRequest(query="vacuum"))
        assert document.title_id in {hit.title_id for hit in outcome.hits}

    async def test_a_name_match_outranks_an_overview_match(self, index: SearchIndex) -> None:
        """**The milestone's central retrieval claim**, and the one no
        membership assertion can see.

        Fails an implementation that concatenates every field into one
        unweighted document -- which is exactly what you get by forgetting
        `setweight`, and which returns both of these titles with identical
        scores. The distractor is deliberately the *more popular* of the two,
        so an unweighted implementation breaking its tie on popularity
        ranks the wrong one first rather than coin-flipping into a pass.
        """
        named = _document("Vacuum", overview="A study of harbour lights.", popularity=1.0)
        mentioned = _document(
            "Harbour Lights",
            overview="A study of the vacuum between two stars.",
            popularity=900.0,
        )
        await self.index_all(index, [mentioned, named])
        outcome = await index.search(SearchRequest(query="vacuum"))
        ranked = [hit.title_id for hit in outcome.hits]
        assert ranked[0] == named.title_id, (
            "an overview match outranked a name match; this is what an "
            "unweighted single-field document produces"
        )
        assert mentioned.title_id in ranked, "the overview match should still be a candidate"

    async def test_a_filter_the_backend_cannot_express_raises(self, index: SearchIndex) -> None:
        """An implementation that silently ignores a filter it does not
        understand and returns a **larger** result set -- which reads as
        working, and is how a two-backend vocabulary drifts into two
        meanings for the same word.

        The first half runs everywhere and is what keeps this case from
        being a no-op against an implementation that expresses the whole
        vocabulary: a filter it *does* express must genuinely exclude.
        """
        movie = _document("Vacuum Chamber", kind=TitleKind.MOVIE)
        series = _document("Vacuum Chamber Diaries", kind=TitleKind.SERIES)
        await self.index_all(index, [movie, series])

        narrowed = await index.search(
            SearchRequest(query="vacuum", filters=SearchFilters(kinds=(TitleKind.MOVIE,)))
        )
        found = {hit.title_id for hit in narrowed.hits}
        assert movie.title_id in found
        assert series.title_id not in found, "an expressed filter must narrow, not decorate"

        if self.unsupported_filter is None:
            pytest.skip("this implementation expresses the whole filter vocabulary")
        # `dict[str, Any]`, not `dict[str, bool]`: the member is named at
        # runtime, so the type checker cannot pair it with its own field and
        # rejects the narrower spelling outright.
        refused: dict[str, Any] = {self.unsupported_filter: True}
        with pytest.raises(FilterNotSupported):
            await index.search(SearchRequest(query="vacuum", filters=SearchFilters(**refused)))

    async def test_indexing_the_same_document_twice_is_one_document(
        self, index: SearchIndex
    ) -> None:
        """A non-idempotent `index_many` that appends, so a redelivery
        doubles every result. PRD 08 makes redelivery safe-by-construction a
        rule *because* the job queue will redeliver -- `requeue_running`
        exists precisely to hand a claimed job to a second worker.

        Two seeded titles, not one: with a single row, "one hit" and "the
        implementation returned exactly one thing" are the same observation.
        """
        document = _document("The Quiet Vacuum")
        other = _document("Vacuum Chamber")
        await self.index_all(index, [document, other])
        await index.index_many([document])
        outcome = await index.search(SearchRequest(query="vacuum", limit=50))
        ranked = [hit.title_id for hit in outcome.hits]
        assert ranked.count(document.title_id) == 1
        assert ranked.count(other.title_id) == 1

    async def test_a_removed_document_is_not_returned(self, index: SearchIndex) -> None:
        """A `remove` that drops the vector and leaves the candidate row, so
        a deleted title keeps appearing with a stale score -- which is why
        `remove` is one method rather than two.

        Searched in `FULL_TEXT` on purpose: a remove that cleared only the
        vector passes a semantic search (the title is gone from that lane
        anyway) and fails here, which is the asymmetry that hides the bug.
        The survivor assertion rules out the other direction, a `remove`
        that empties the index.
        """
        removed = _document("The Quiet Vacuum")
        survivor = _document("Vacuum Chamber")
        await self.index_all(index, [removed, survivor])
        await index.remove(removed.title_id)
        outcome = await index.search(SearchRequest(query="vacuum", limit=50))
        found = {hit.title_id for hit in outcome.hits}
        assert removed.title_id not in found
        assert survivor.title_id in found

    # --- semantic ----------------------------------------------------------

    async def test_semantic_search_uses_the_supplied_query_vector(self, index: SearchIndex) -> None:
        """An implementation that re-embeds the query itself -- which is the
        whole reason `query_vector` moved onto the request. A backend doing
        its own embedding is a backend with its own model, its own prefix
        convention (see `Embedder`'s -0.0663) and its own drift, and nothing
        in the stale predicate can see any of it.

        **Two searches with the identical query string and opposite
        vectors.** An implementation that embedded the string cannot produce
        both answers, whatever model it holds. Neither document's text
        contains the query at all, so a full-text fallback also fails.
        """
        if not self.supports_semantic:
            pytest.skip("this implementation cannot express a supplied query vector")
        east = _document("Harbour Lights", vector=(1.0, 0.0))
        north = _document("Vacuum Chamber", vector=(0.0, 1.0))
        await self.index_all(index, [east, north])

        first = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=(1.0, 0.0)
            )
        )
        second = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=(0.0, 1.0)
            )
        )
        assert first.hits[0].title_id == east.title_id
        assert second.hits[0].title_id == north.title_id

    async def test_a_title_with_no_vector_is_absent_from_semantic_results_not_last(
        self, index: SearchIndex
    ) -> None:
        """The trap in point 3 of "the one thing this milestone must not get
        wrong": an implementation that treats a missing vector as a **zero
        vector**, which makes every unembedded title a mediocre match for
        every query instead of a non-candidate.

        Arranged so that failure is *first place*, not last. The query
        vector is orthogonal to the embedded title, so it scores 0.0 -- and
        a zero-vector implementation scores the unembedded title 0.0 too,
        ties, and breaks the tie on popularity, which the unembedded title
        wins by three orders of magnitude. `semantic_coverage` is asserted
        alongside, because a caller has no other way to learn that half its
        catalog was never a candidate.
        """
        if not self.supports_semantic:
            pytest.skip("this implementation cannot express a supplied query vector")
        embedded = _document("Harbour Lights", vector=(1.0, 0.0), popularity=1.0)
        unembedded = _document("Vacuum Chamber", vector=None, popularity=900.0)
        await self.index_all(index, [embedded, unembedded])
        outcome = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=(0.0, 1.0)
            )
        )
        found = [hit.title_id for hit in outcome.hits]
        assert unembedded.title_id not in found, (
            "a title with no vector was returned; a missing vector is not a zero vector"
        )
        assert embedded.title_id in found
        assert outcome.semantic_coverage == pytest.approx(0.5)

    # --- fusion ------------------------------------------------------------

    async def test_fusion_produces_an_order_neither_input_produced(
        self, index: SearchIndex
    ) -> None:
        """An implementation whose `FUSED` mode returns one lane and ignores
        the other -- indistinguishable from working unless the seeded lanes
        disagree, which is why this case runs both lanes first and asserts
        what each returns before fusing them.

        The winner is first in **neither** lane: `text` wins full-text and
        appears nowhere in the vector lane (it has no vector at all),
        `vector` wins the vector lane and matches no text, and `both` is
        second in each. Reciprocal rank fusion at k=60 gives `both`
        1/62 + 1/62 against 1/61 for each single-lane leader -- a 2x margin,
        so this does not rest on float noise the way a symmetric
        three-document arrangement would.

        **`text` carrying a vector is what the plan's draft got wrong, and
        the margin is why it matters.** With `text` also a rank-3 candidate
        in the vector lane it scores 1/61 + 1/63 = 0.0322665 against `both`'s
        0.0322581 -- correct RRF, and the wrong answer, decided at the eighth
        decimal place. A case whose expected order depends on a difference
        that small is not asserting the property it claims to.
        """
        if not self.supports_semantic:
            pytest.skip("this implementation cannot express a supplied query vector")
        text = _document("Vacuum Chamber", popularity=1.0)
        both = _document(
            "Harbour Lights", overview="Inside the vacuum.", vector=(0.8, 0.6), popularity=1.0
        )
        vector = _document("Salt Flats", vector=(1.0, 0.0), popularity=1.0)
        await self.index_all(index, [text, both, vector])

        lexical = await index.search(SearchRequest(query="vacuum", mode=SearchMode.FULL_TEXT))
        assert lexical.hits[0].title_id == text.title_id
        semantic = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.SEMANTIC, query_vector=(1.0, 0.0))
        )
        assert semantic.hits[0].title_id == vector.title_id

        fused = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.FUSED, query_vector=(1.0, 0.0))
        )
        assert fused.hits[0].title_id == both.title_id, (
            "fusion returned a lane's own winner; the two lanes were seeded to disagree "
            "precisely so that returning either one fails"
        )

    async def test_fusion_does_not_add_scores_from_different_scales(
        self, index: SearchIndex
    ) -> None:
        """Weighted score addition wearing RRF's name. ADR-0002: fuse by
        rank, "never by adding scores from incompatible scales".

        Seeded so the two rules give **opposite** answers. `strong` is the
        vector lane's clear winner (cosine 1.0) and is not a text candidate
        at all; `weak` is the text lane's only hit with a deliberately tiny
        score -- a single overview mention, which is `ts_rank` territory of
        ~0.06 on real Postgres -- and is second in the vector lane at cosine
        0.2. Addition gives `strong` 1.0 against `weak`'s ~0.26 and puts
        `strong` first; RRF gives `weak` 1/61 + 1/62 against `strong`'s 1/61
        and puts `weak` first. An implementation that adds cannot pass this,
        and one that adds *with weights tuned until this passes* has tuned
        itself into rank fusion the hard way.
        """
        if not self.supports_semantic:
            pytest.skip("this implementation cannot express a supplied query vector")
        strong = _document("Salt Flats", vector=(1.0, 0.0), popularity=1.0)
        weak = _document(
            "Harbour Lights",
            overview="A vacuum, mentioned once.",
            vector=(0.2, 0.9797958971132712),
            popularity=1.0,
        )
        await self.index_all(index, [strong, weak])

        lexical = await index.search(SearchRequest(query="vacuum", mode=SearchMode.FULL_TEXT))
        assert [hit.title_id for hit in lexical.hits] == [weak.title_id]
        semantic = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.SEMANTIC, query_vector=(1.0, 0.0))
        )
        assert semantic.hits[0].title_id == strong.title_id

        fused = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.FUSED, query_vector=(1.0, 0.0))
        )
        assert fused.hits[0].title_id == weak.title_id, (
            "the lane with one large score won; that is score addition, not RRF"
        )
