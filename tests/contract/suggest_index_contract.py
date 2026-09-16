"""What every `SuggestIndex` owes the type-ahead box, and what only typo tolerance adds."""

import uuid

import pytest

from usher.ports.search import SuggestIndex


class SuggestIndexContract:
    """The two properties every `SuggestIndex` owes, tier or no tier."""

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        """Seed one title and return its id."""
        raise NotImplementedError

    async def test_a_prefix_returns_the_title_that_starts_with_it(
        self, index: SuggestIndex
    ) -> None:
        """A prefix finds the title that starts with it.

        Asserts position rather than membership even here, because the distractor is
        seeded first and a physical-order implementation would return it first.
        """
        await self.given_title(index, name="Vacuum Chamber", popularity=900.0)
        wanted = await self.given_title(index, name="Harbour Lights", popularity=1.0)
        hits = await index.suggest("harb")
        assert hits[0].title_id == wanted

    async def test_results_are_ordered_by_popularity_within_equal_distance(
        self, index: SuggestIndex
    ) -> None:
        """Equally-good matches are ordered by popularity, not by physical order.

        The two names are *exactly* equidistant from the prefix by construction, so
        distance cannot decide. The unpopular one is inserted first, and that premise
        is asserted rather than described: ids are UUIDv7 minted at insert time, so a
        fixture seeded the other way round would make `ORDER BY popularity` and
        `ORDER BY id` agree and let an implementation with neither key pass.
        """
        first = await self.given_title(index, name="Vane Alpha", popularity=1.0)
        popular = await self.given_title(index, name="Vane Bravo", popularity=900.0)
        assert first < popular, "the premise: insertion order and popularity order disagree"
        hits = await index.suggest("vane")
        assert len(hits) >= 2
        assert hits[0].title_id == popular


class TypoTolerantSuggestIndexContract(SuggestIndexContract):
    """The three cases that are claims about `pg_trgm` and `levenshtein`.

    Two are typo-tolerance weaknesses asserted rather than assumed; the third is the
    latency cliff the candidate cap exists for. `PostgresPrefixSuggestIndex`
    deliberately does not subclass this -- its near-total absence of typo recall is
    asserted in `tests/integration/test_adapters_search_prefix.py`, positively
    controlled so it cannot pass by never running.
    """

    # `FakeSuggestIndex` runs `levenshtein` over its whole dict, so the one
    # property the real path exists for -- capping candidates before the
    # re-rank -- is structurally absent from it. Skipped, never passed: a
    # pass would be a claim about a latency cliff that a dict cannot make.
    supports_candidate_cap: bool = False
    candidate_cap: int = 200

    async def rerank_candidates(self, index: SuggestIndex) -> int:
        """How many rows the last `suggest` ran its distance function over.

        Only called when `supports_candidate_cap` is set. On Postgres this comes
        from the plan of the statement the implementation issued, not from a clock:
        a wall-clock assertion on a warm 500-row fixture says more about the host
        than about the index.
        """
        raise NotImplementedError

    async def test_a_single_character_typo_still_finds_a_short_title(
        self, index: SuggestIndex
    ) -> None:
        """A one-character typo still finds a short title.

        Fails a pure `LIKE 'prefix%'` implementation, which finds nothing at all for
        a misspelt prefix, and a pure trigram implementation with no `levenshtein`
        re-rank, whose overlap on a four-character name is one trigram or none. The
        distractor shares no characters with the query and is far more popular, so an
        implementation whose candidate predicate silently matches everything, leaving
        popularity to order the whole table, puts it first.
        """
        await self.given_title(index, name="Harbour Lights", popularity=900.0)
        wanted = await self.given_title(index, name="Vane", popularity=1.0)
        hits = await index.suggest("vame")
        assert hits, "a one-character typo returned nothing; this is the LIKE implementation"
        assert hits[0].title_id == wanted

    async def test_a_transposition_still_finds_a_short_title(self, index: SuggestIndex) -> None:
        """A transposition still finds a short title: trigram overlap's blind spot.

        `"vnae"` and `"vane"` share **no trigram at all** ({vna, nae} against
        {van, ane}), so `similarity()` is 0.0 and a trigram-only candidate predicate
        cannot see this title however low its threshold. Levenshtein distance is 2.
        Same distractor, same reason.
        """
        await self.given_title(index, name="Harbour Lights", popularity=900.0)
        wanted = await self.given_title(index, name="Vane", popularity=1.0)
        hits = await index.suggest("vnae")
        assert hits, "a transposition returned nothing; trigram overlap here is exactly zero"
        assert hits[0].title_id == wanted

    async def test_the_candidate_set_is_capped_before_the_rerank(self, index: SuggestIndex) -> None:
        """The candidate set is capped before the re-rank.

        An implementation running `levenshtein` over the whole table walks into the
        latency cliff `levenshtein_less_equal` exists to avoid. Asserted against the
        query plan rather than a wall clock, and skipped by the fake, which runs
        distance over its whole dict and therefore has *better* typo tolerance than
        the real path -- the dangerous direction, and the reason this is a skip.
        """
        if not self.supports_candidate_cap:
            pytest.skip("this implementation cannot cap its candidate set")
        seeded = self.candidate_cap * 3
        for number in range(seeded):
            await self.given_title(index, name=f"Vane {number:04d}", popularity=1.0)
        await index.suggest("vane")
        examined = await self.rerank_candidates(index)
        assert examined <= self.candidate_cap
        assert examined < seeded
