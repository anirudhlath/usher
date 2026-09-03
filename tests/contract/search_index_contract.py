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

        async def given_title_row(self, document: SearchDocument) -> None:
            return None
"""

from collections.abc import Sequence
from typing import Any

import pytest

from usher.db.models.search import EMBEDDING_DIMENSIONS
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

# Every vector in this file is this wide, and the reason is that a shared
# contract has to be storable by every implementation running it.
# `SearchDocument.vector` is `tuple[float, ...]` -- the port declares no
# width, deliberately, because a width is a property of a model -- but
# `title_embeddings.embedding` is `halfvec(384)` and pgvector rejects
# anything else with `expected 384 dimensions, not 2`. So the two-component
# vectors these cases are *arranged* around are zero-padded to the shipped
# width, which changes no assertion in this file: padding both a document
# and a query with zeros leaves every dot product, every norm and therefore
# every cosine exactly as it was.
#
# The number lives here rather than being imported from
# `usher.db.models.search`: a port-level contract that reached into `db/` to
# learn how wide a vector is would be the contract knowing about one
# backend, which is the thing this file exists not to do.
_VECTOR_DIMENSIONS = EMBEDDING_DIMENSIONS


def _vector(*components: float) -> tuple[float, ...]:
    """One of this file's arranged vectors, widened to the shipped width."""
    return components + (0.0,) * (_VECTOR_DIMENSIONS - len(components))


def _document(
    name: str,
    *,
    overview: str | None = None,
    genres: tuple[str, ...] = (),
    kind: TitleKind = TitleKind.MOVIE,
    popularity: float = 1.0,
    year: int | None = 2019,
    vector: tuple[float, ...] | None = None,
    credits: tuple[str, ...] = (),
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
        credits=credits,
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

    # Whether this backend owns the *document's* lifecycle as well as the
    # vector's. `FakeSearchIndex` and any document store do: the document is
    # a row they wrote and can delete. `PostgresSearchIndex` does not --
    # `titles.search_document` is a generated column of a table the catalog
    # owns, so a title that exists is full-text indexed by construction, and
    # an index that deleted `titles` rows to satisfy `remove` would turn a
    # reindex bug into data loss.
    #
    # Declared `True` by default so the *stronger* assertion is what a new
    # driver gets for free and weakening it is a visible line in a subclass.
    # The Postgres driver pays for the exemption with
    # `test_deleting_the_title_removes_it_from_full_text`, which asserts the
    # same property through the mechanism that really owns it.
    owns_document_lifecycle: bool = True

    async def given_title_row(self, document: SearchDocument) -> None:
        """Arrangement a real index constrains and a dict does not.

        **Takes the whole document, not just its id.** A hook handed an id
        alone is one no real backend can satisfy: on Postgres the document's
        text is a fact about the `titles` row -- `search_document` is
        `GENERATED ALWAYS AS (...) STORED` over `name`, `overview`, `genres`
        and `keywords` -- so a driver that could only insert an id would seed
        an empty document and every retrieval case in this file would assert
        against blank text.

        `FakeSearchIndex` is a dict with no foreign keys and needs nothing.
        A hook rather than a `titles` insert in every case, for the reason
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
            await self.given_title_row(document)
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

    async def test_a_cast_match_ranks_below_a_name_match_and_above_an_overview_match(
        self, index: SearchIndex
    ) -> None:
        """Weight class B, and **`test_a_name_match_outranks_an_overview_match`
        is not enough to pin it.**

        That case compares A against C, and B sits between them without
        touching either -- so a milestone that filled B with the wrong weight
        passes it. Two wrong implementations this kills, and both are one
        character in a migration: class B filled at `'A'` (the cast match ties
        the name match, and this is an *ordering* assertion rather than a
        membership one) and class B filled at `'D'` (it falls below the
        overview match).

        Measured on pg17.10 before this case was written, three rows carrying
        `Marlow Vance` in three different classes, scored with
        `ts_rank(search_document, websearch_to_tsquery('english', 'marlow
        vance'))`: name **0.9910322**, credit_names **0.39641288**, overview
        **0.19820644**. A is 2.5x B and B is 2x C, with no ties and no tuning
        -- those are `ts_rank`'s default weights `{0.1, 0.2, 0.4, 1.0}` doing
        exactly what the class assignment says.

        All three rows are in one index and the assertion is on **position**,
        which is the front matter's rule 1 and is what distinguishes "B is
        between A and C" from "B exists".
        """
        # **Creation order is the load-bearing half, and it is the id order
        # rather than the list order that matters** -- `_document` mints a
        # UUIDv7 when it is *called*. Measured on pg17.10: class B filled at
        # `'A'` makes the cast row and the name row tie *exactly*, at
        # 0.9910322 both, so which one comes back first is decided entirely by
        # the tiebreak -- `ORDER BY score DESC, t.id` in the shipped statement
        # and `title_id.bytes` in the fake, both ascending. So `named` is
        # minted **last**: on a tie it sorts behind `credited` and the case
        # fails. Minted first, the tie resolves to the expected answer and the
        # `'A'` mutation survives -- verified, it did, before this ordering
        # was fixed.
        mentioned = _document("Ten Harbour", overview="Marlow Vance walks at dusk.")
        credited = _document(
            "Nine Harbour", overview="A harbour at dusk.", credits=("Marlow Vance",)
        )
        named = _document("Marlow Vance", overview="A harbour at dusk.")
        await self.index_all(index, [mentioned, credited, named])

        outcome = await index.search(SearchRequest(query="marlow vance"))

        assert [hit.title_id for hit in outcome.hits] == [
            named.title_id,
            credited.title_id,
            mentioned.title_id,
        ]

    async def test_a_name_match_outranks_an_overview_match(self, index: SearchIndex) -> None:
        """**The milestone's central retrieval claim**, and the one no
        membership assertion can see.

        Fails an implementation that concatenates every field into one
        unweighted document -- which is exactly what you get by forgetting
        `setweight`, and which returns both of these titles with identical
        scores. The distractor is deliberately the *more popular* of the two,
        so an unweighted implementation breaking its tie on popularity
        ranks the wrong one first rather than coin-flipping into a pass.

        **And it is minted first, which is the half a Postgres backend
        needs.** Every id here is a UUIDv7, so creation order *is* id order,
        and both drivers break a score tie on the id -- the fake in `_rank`,
        the shipped statement in `ORDER BY score DESC, t.id`. With the
        distractor created second, `mentioned.title_id` sorts after
        `named.title_id` and the expected answer is also what a pure
        `ORDER BY t.id` produces: measured, both `_WEIGHTS = (1, 1, 1, 1)`
        and deleting `ORDER BY score DESC` outright *survived* this case in
        that arrangement. Creating the distractor first is what makes id
        order disagree with the right answer, which is the whole of "a
        relevance assertion any ordering satisfies is not a relevance test".
        """
        mentioned = _document(
            "Harbour Lights",
            overview="A study of the vacuum between two stars.",
            popularity=900.0,
        )
        named = _document("Vacuum", overview="A study of harbour lights.", popularity=1.0)
        await self.index_all(index, [mentioned, named])
        outcome = await index.search(SearchRequest(query="vacuum"))
        ranked = [hit.title_id for hit in outcome.hits]
        assert ranked[0] == named.title_id, (
            "an overview match outranked a name match; this is what an "
            "unweighted single-field document produces"
        )
        assert mentioned.title_id in ranked, "the overview match should still be a candidate"

    async def test_a_title_named_exactly_the_query_leads_a_longer_document_repeating_it(
        self, index: SearchIndex
    ) -> None:
        """**Issue #25, in the lane that decides it.**

        `GET /search?q=The Matrix` returned the 1999 film 5th behind three 2018
        video essays repeating the phrase in their own names, and the blend
        could not rescue it: no combination of popularity, ownership, watch
        state, recency and taste can overturn dense rank 0 (margin
        `0.005 / 1.045` = 0.004785 with all six present, deliberate, and the
        bound F5's taste weight is derived from -- 0.009615, which this
        docstring carried until 2026-09-02, is the same bound with taste
        *absent*). So the lane has to put the right row there.

        **The premise is asserted from the hits themselves and it is what makes
        this a relevance test**: the essay's own index score is strictly
        *higher* -- it repeats the query and carries the words twice more in
        its overview -- so an implementation that orders by score alone puts it
        first, which is the shipped behaviour this case exists to fail. Both
        drivers reproduce that ordering for their own reasons (real
        `ts_rank_cd` on Postgres; the name plus prose weight classes in the
        fake), and neither is asked to agree on the *value*.

        The essay is created first and is 900x the more popular, so the two
        tiebreaks a wrong implementation falls back on -- id ascending, then
        popularity -- both point at the wrong answer rather than coin-flipping
        into a pass.
        """
        essay = _document(
            "Vacuum for Realists (aka Reviewing Vacuum in Terms of One Cypher)",
            overview="A vacuum, reviewed at length, in a vacuum.",
            popularity=900.0,
        )
        named = _document("Vacuum", overview="A study of harbour lights.", popularity=1.0)
        await self.index_all(index, [essay, named])

        outcome = await index.search(SearchRequest(query="vacuum"))

        ranked = [hit.title_id for hit in outcome.hits]
        scores = {hit.title_id: hit.score for hit in outcome.hits}
        assert scores[essay.title_id] > scores[named.title_id], (
            "the premise: the longer document outscores the exact name on this backend's "
            "own text score, which is the whole defect -- without it any sort passes"
        )
        assert essay.title_id < named.title_id, (
            "the premise: creation order is id order, so the id tiebreak also points at the essay"
        )
        assert ranked[0] == named.title_id, (
            "a longer document repeating the query outranked the title the query names"
        )

    async def test_only_the_title_named_exactly_the_query_is_flagged_as_an_exact_name(
        self, index: SearchIndex
    ) -> None:
        """The flag the blend reads, and **the mutation that matters is the
        generous one.**

        `SearchService._dense_ranks` groups by `(exact_name, score)`, so a flag
        set on every hit alike is indistinguishable from no flag at all -- the
        rows tie again and popularity decides -- while a flag set on nothing is
        the shipped defect. Only asserting *both* arms catches both, which is
        why the near-miss row here is a **prefix** match rather than an
        unrelated one: `Vacuum Chamber` starts with the whole query, which is
        exactly what tier-1 suggest matches on, and carrying that tier's rule
        over unchanged would flag it too.

        Case-insensitively, because the query is what somebody typed and the
        catalog's own casing is not theirs to guess -- `lower()` on both sides
        in the statement, `casefold()` in the fake.
        """
        named = _document("Vacuum", popularity=1.0)
        prefixed = _document("Vacuum Chamber", popularity=900.0)
        await self.index_all(index, [named, prefixed])

        outcome = await index.search(SearchRequest(query="VACUUM"))

        flagged = {hit.title_id for hit in outcome.hits if hit.exact_name}
        assert prefixed.title_id in {hit.title_id for hit in outcome.hits}, (
            "the premise: the near-miss row is a candidate, so its flag is a real answer"
        )
        assert flagged == {named.title_id}

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
        """A `remove` that drops one half of a title's index state and leaves
        the other, so a deleted title keeps appearing with a stale score --
        which is why `remove` is one method rather than two.

        **Which half is asserted depends on which half the backend owns**,
        and neither driver is let off. A document store owns both, so this
        searches `FULL_TEXT` on purpose: a `remove` that cleared only the
        vector passes a semantic search (the title is gone from that lane
        anyway) and fails here, which is the asymmetry that hides the bug.
        Postgres owns the vector only -- its document is a generated column
        of a table this port does not own -- so it asserts the semantic half
        here and the full-text half through `ON DELETE CASCADE` in
        `tests/integration/test_adapters_search_postgres.py`.

        Both documents carry a vector so the semantic branch has two
        candidates to distinguish; the survivor assertion rules out the other
        direction, a `remove` that empties the index.
        """
        removed = _document("The Quiet Vacuum", vector=_vector(1.0, 0.0))
        survivor = _document("Vacuum Chamber", vector=_vector(0.6, 0.8))
        await self.index_all(index, [removed, survivor])
        await index.remove(removed.title_id)

        if self.owns_document_lifecycle:
            outcome = await index.search(SearchRequest(query="vacuum", limit=50))
        elif self.supports_semantic:
            outcome = await index.search(
                SearchRequest(
                    query="vacuum",
                    mode=SearchMode.SEMANTIC,
                    query_vector=_vector(1.0, 0.0),
                    limit=50,
                )
            )
        else:
            pytest.skip("this driver owns neither lane's lifecycle yet; see Task 18")
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
        east = _document("Harbour Lights", vector=_vector(1.0, 0.0))
        north = _document("Vacuum Chamber", vector=_vector(0.0, 1.0))
        await self.index_all(index, [east, north])

        first = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=_vector(1.0, 0.0)
            )
        )
        second = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=_vector(0.0, 1.0)
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
        embedded = _document("Harbour Lights", vector=_vector(1.0, 0.0), popularity=1.0)
        unembedded = _document("Vacuum Chamber", vector=None, popularity=900.0)
        await self.index_all(index, [embedded, unembedded])
        outcome = await index.search(
            SearchRequest(
                query="unrelated words", mode=SearchMode.SEMANTIC, query_vector=_vector(0.0, 1.0)
            )
        )
        found = [hit.title_id for hit in outcome.hits]
        assert unembedded.title_id not in found, (
            "a title with no vector was returned; a missing vector is not a zero vector"
        )
        assert embedded.title_id in found
        assert outcome.semantic_coverage == pytest.approx(0.5)

    async def test_coverage_is_answerable_before_a_query_vector_exists(
        self, index: SearchIndex
    ) -> None:
        """**The number `search` reports, askable without a search** -- which
        is what makes it usable as a guard in front of the embed rather than
        only as a report after it (issue #16).

        Two claims, and the second is the one an implementation can get wrong
        while looking right. First, it takes **filters and no vector**: PRD
        09's carried-debt entry recorded the filtered predicate as *"not
        answerable before the vector that does the filtering exists"*, and it
        is -- nothing in a `SearchFilters` is derived from a query vector.
        Second, it is the **filtered** population and not the whole catalog:
        the two agree on any arrangement where the filter matches everything,
        so the narrowing case below is the only thing that separates them.

        Fails against an implementation answering over the whole catalog
        (`0.5` in the second assertion), and against one deriving coverage
        from hits it has not got (`0.0` or a `ZeroDivisionError` in the
        first).
        """
        if not self.supports_semantic:
            pytest.skip("this implementation stores no vectors to have coverage of")
        embedded = _document("Harbour Lights", vector=_vector(1.0, 0.0), kind=TitleKind.MOVIE)
        unembedded = _document("Vacuum Chamber", vector=None, kind=TitleKind.SERIES)
        await self.index_all(index, [embedded, unembedded])

        assert await index.semantic_coverage(SearchFilters()) == pytest.approx(0.5)
        assert await index.semantic_coverage(
            SearchFilters(kinds=(TitleKind.SERIES,))
        ) == pytest.approx(0.0), "coverage was measured over the catalog, not over the filtered set"
        assert await index.semantic_coverage(
            SearchFilters(kinds=(TitleKind.MOVIE,))
        ) == pytest.approx(1.0)

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
            "Harbour Lights",
            overview="Inside the vacuum.",
            vector=_vector(0.8, 0.6),
            popularity=1.0,
        )
        vector = _document("Salt Flats", vector=_vector(1.0, 0.0), popularity=1.0)
        await self.index_all(index, [text, both, vector])

        lexical = await index.search(SearchRequest(query="vacuum", mode=SearchMode.FULL_TEXT))
        assert lexical.hits[0].title_id == text.title_id
        semantic = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.SEMANTIC, query_vector=_vector(1.0, 0.0))
        )
        assert semantic.hits[0].title_id == vector.title_id

        fused = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.FUSED, query_vector=_vector(1.0, 0.0))
        )
        assert fused.hits[0].title_id == both.title_id, (
            "fusion returned a lane's own winner; the two lanes were seeded to disagree "
            "precisely so that returning either one fails"
        )

    async def test_fusion_puts_an_exact_name_match_first_even_when_it_fuses_lower(
        self, index: SearchIndex
    ) -> None:
        """**The exact-name key survives fusion, and RRF is exactly what would
        lose it.**

        A title in *both* lanes beats a title in one, arithmetically and
        always: `1/62 + 1/61` against `1/61`, whatever either lane thought of
        either row. So the row whose name **is** the query -- and which has no
        vector, like nine titles in ten on this catalog -- fuses **below** a
        near-match that placed in both, and `_dense_ranks` would hand it dense
        rank 1 where the other five signals can bury it again (issue #25).

        The premise is the fused scores themselves, asserted from the hits: the
        distractor's is strictly higher, so this is not satisfied by any
        ordering by score. It is also 900x the more popular and is created
        second, so neither tiebreak rescues a wrong implementation.

        Kills three mutants the full-text cases cannot see: the exact-name
        column dropped from the fused projection, the outer `ORDER BY` left on
        `score DESC` alone, and `COALESCE(lexical.exact_name, false)` written
        without its `COALESCE` -- a NULL from the vector-only arm sorts
        **first** under `DESC`, which is trap 1 of this statement's own list
        arriving through a new column.
        """
        if not self.supports_semantic:
            pytest.skip("this implementation cannot express a supplied query vector")
        named = _document("Vacuum", popularity=1.0)
        both = _document(
            "Vacuum Chamber",
            overview="Inside the vacuum, a vacuum.",
            vector=_vector(1.0, 0.0),
            popularity=900.0,
        )
        await self.index_all(index, [named, both])

        fused = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.FUSED, query_vector=_vector(1.0, 0.0))
        )

        scores = {hit.title_id: hit.score for hit in fused.hits}
        assert scores[both.title_id] > scores[named.title_id], (
            "the premise: the two-lane row fuses higher, which is what makes this an "
            "ordering assertion rather than a restatement of the fused score"
        )
        assert fused.hits[0].title_id == named.title_id
        assert fused.hits[0].exact_name, "the flag has to survive the fusion, not just the lane"
        assert not fused.hits[1].exact_name

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
        strong = _document("Salt Flats", vector=_vector(1.0, 0.0), popularity=1.0)
        weak = _document(
            "Harbour Lights",
            overview="A vacuum, mentioned once.",
            vector=_vector(0.2, 0.9797958971132712),
            popularity=1.0,
        )
        await self.index_all(index, [strong, weak])

        lexical = await index.search(SearchRequest(query="vacuum", mode=SearchMode.FULL_TEXT))
        assert [hit.title_id for hit in lexical.hits] == [weak.title_id]
        semantic = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.SEMANTIC, query_vector=_vector(1.0, 0.0))
        )
        assert semantic.hits[0].title_id == strong.title_id

        fused = await index.search(
            SearchRequest(query="vacuum", mode=SearchMode.FUSED, query_vector=_vector(1.0, 0.0))
        )
        assert fused.hits[0].title_id == weak.title_id, (
            "the lane with one large score won; that is score addition, not RRF"
        )
