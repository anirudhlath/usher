"""What every `SuggestIndex` implementation owes the type-ahead box.

Runs against `FakeSuggestIndex` and `PostgresSuggestIndex`. Five cases, and
three of them are ADR-0002's own stated weaknesses asserted rather than
assumed -- an ADR that names a risk and ships no case for it has recorded a
worry, not managed one.

**The port has no write method** (ADR-0021: adding one is how the dual write
arrives without a decision), so arrangement cannot go through the port at
all. `given_title` is therefore a hook rather than a convenience: the fake
writes into its own dict through a test-only method, and Postgres inserts a
`titles` row, which is the honest shape of a port that reads a table
somebody else owns.
"""

import uuid

import pytest

from usher.ports.search import SuggestIndex


class SuggestIndexContract:
    # `FakeSuggestIndex` runs `levenshtein` over its whole dict, so the one
    # property the real path exists for -- capping candidates before the
    # re-rank -- is structurally absent from it. Skipped, never passed: a
    # pass would be a claim about a latency cliff that a dict cannot make.
    supports_candidate_cap: bool = False
    candidate_cap: int = 200

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        """Seed one title and return its id."""
        raise NotImplementedError

    async def rerank_candidates(self, index: SuggestIndex) -> int:
        """How many rows the last `suggest` ran its distance function over.

        Only called when `supports_candidate_cap` is set. On Postgres this
        comes from the plan of the statement the implementation issued, not
        from a clock: a wall-clock assertion on a warm 500-row fixture
        measures the host's mood, and PRD 05's cliff is about 1.27M rows.
        """
        raise NotImplementedError

    async def test_a_prefix_returns_the_title_that_starts_with_it(
        self, index: SuggestIndex
    ) -> None:
        """The empty implementation. Asserts position rather than
        membership even here, because the distractor is seeded first and a
        physical-order implementation would return it first."""
        await self.given_title(index, name="Vacuum Chamber", popularity=900.0)
        wanted = await self.given_title(index, name="Harbour Lights", popularity=1.0)
        hits = await index.suggest("harb")
        assert hits[0].title_id == wanted

    async def test_a_single_character_typo_still_finds_a_short_title(
        self, index: SuggestIndex
    ) -> None:
        """**ADR-0002's known genuine weakness, asserted rather than
        assumed.** Fails a pure `LIKE 'prefix%'` implementation, which finds
        nothing at all for a misspelt prefix, and a pure trigram
        implementation with no `levenshtein` re-rank, whose overlap on a
        four-character name is one trigram or none.

        The distractor shares no characters with the query and is 900x more
        popular, so an implementation that returns its whole table ordered
        by popularity -- the shape you get when the candidate predicate
        silently matches everything -- puts it first.

        Synthetic short titles; the real-catalog version, over real names
        with real neighbours, is the Meilisearch gate's own measurement.
        """
        await self.given_title(index, name="Harbour Lights", popularity=900.0)
        wanted = await self.given_title(index, name="Vane", popularity=1.0)
        hits = await index.suggest("vame")
        assert hits, "a one-character typo returned nothing; this is the LIKE implementation"
        assert hits[0].title_id == wanted

    async def test_a_transposition_still_finds_a_short_title(self, index: SuggestIndex) -> None:
        """Trigram overlap's near-blind spot, named explicitly in ADR-0002
        and asserted here rather than trusted.

        `"vnae"` and `"vane"` share **no trigram at all** ({vna, nae} against
        {van, ane}), so `similarity()` is 0.0 and a trigram-only candidate
        predicate cannot see this title however low its threshold. Levenshtein
        distance is 2. Same distractor, same reason.
        """
        await self.given_title(index, name="Harbour Lights", popularity=900.0)
        wanted = await self.given_title(index, name="Vane", popularity=1.0)
        hits = await index.suggest("vnae")
        assert hits, "a transposition returned nothing; trigram overlap here is exactly zero"
        assert hits[0].title_id == wanted

    async def test_results_are_ordered_by_popularity_within_equal_distance(
        self, index: SuggestIndex
    ) -> None:
        """An implementation that returns candidates in physical order, so
        the type-ahead box's first row is arbitrary among equally-good
        matches -- which on a household catalog means the obvious answer is
        second about half the time and nobody can reproduce it.

        The two names are *exactly* equidistant from the prefix by
        construction, so distance cannot decide, and the unpopular one is
        inserted first so insertion order and the right answer disagree.
        """
        await self.given_title(index, name="Vane Alpha", popularity=1.0)
        popular = await self.given_title(index, name="Vane Bravo", popularity=900.0)
        hits = await index.suggest("vane")
        assert len(hits) >= 2
        assert hits[0].title_id == popular

    async def test_the_candidate_set_is_capped_before_the_rerank(self, index: SuggestIndex) -> None:
        """An implementation running `levenshtein` over the whole table --
        the exact latency cliff PRD 05 says the narrow path exists to avoid,
        and the reason `levenshtein_less_equal` exists at all.

        Asserted by measured work rather than by wall clock: a timing
        assertion over a 500-row fixture measures the host, and the cliff is
        a property of 1.27M rows. Skipped by the fake, which computes
        distance over its whole dict and therefore has *better* typo
        tolerance than the real path -- the dangerous direction, and the
        reason this is a skip and not a pass.
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
