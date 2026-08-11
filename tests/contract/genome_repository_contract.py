"""Behaviour every `GenomeRepository` implementation must satisfy.

Ten cases, and most of them are about a value that is *wrong and plausible*
rather than about a value that is missing -- which is what M7's opening
section says a genome vector's failures look like. A zero vector, a padded
missing side, and a cosine taken across two releases all produce a number, in
range, with nothing to distinguish it from a right one.

**M8 Task 19 added the vocabulary half, where the same failure is worse.** A
cosine taken across two releases is a wrong *number*; a lane name taken across
two releases is a sentence about a household's taste, in prose, on a screen.
So `get_pair` answers `None` across a mismatch and `vocabulary` raises, and
the four vocabulary cases below are the argument for that asymmetry.

**This port has no writer**, so the suite seeds through an abstract
`GenomeSeeder` the two arms implement (a raw `INSERT` for Postgres, a dict
for the fake) -- the same shape `tests/contract/source_harness.py` uses. Its
`ABC` shape is ADR-0001's argument applied to a test double: a `Protocol`
would let one arm drift out of the suite silently. Do not add a `put()` to
the port to make seeding convenient.

**Every case names the wrong implementation it rules out.**

Subclass and provide `repository` and `seeder`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

import pytest

from usher.ports.bulk import GENOME_TAG_COUNT
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import GenomeRepository

#: The production width, not a convenient small one. `halfvec(1128)` rejects
#: a vector of any other length, so a narrower contract vector would have to
#: be padded by one arm's seeder and not the other's -- and the padding would
#: then be this suite's behaviour rather than the port's. `lanes()` is what
#: keeps a case readable at that width.
WIDTH = GENOME_TAG_COUNT

#: Two release tokens. Opaque strings, exactly as the archive's ETag is --
#: the point of the second one is only that it differs from the first.
RELEASE_A = "an-invented-etag-a"
RELEASE_B = "an-invented-etag-b"


def lanes(*values: float) -> tuple[float, ...]:
    """A vector of `WIDTH` lanes, padded with zeros on the right.

    Written as a helper so a case can name only the lanes it cares about and
    still store a full-width vector -- a short vector is rejected outright by
    `halfvec(WIDTH)` on one arm and silently accepted on the other, and that
    divergence would be this suite's, not the port's.
    """
    return tuple(values) + (0.0,) * (WIDTH - len(values))


class GenomeSeeder(ABC):
    """A `genome_scores` row, written by whatever the implementation stores
    into. The port cannot write one and deliberately never will."""

    @abstractmethod
    async def title(self) -> uuid.UUID:
        """A title with no genome vector, returning its id.

        Separate from `vector` because "a title that exists and has no
        vector" is the population 98.7% of the catalog is in, and a case that
        used a freshly-minted UUID naming no title at all would be testing a
        different thing -- one arm has a foreign key and the other does not.
        """

    @abstractmethod
    async def vector(
        self, title_id: uuid.UUID, relevance: tuple[float, ...], *, revision: str = RELEASE_A
    ) -> None:
        """Store one vector for an existing title."""

    @abstractmethod
    async def tags(self, tags: Sequence[tuple[int, str]], *, revision: str = RELEASE_A) -> None:
        """Store a `genome_tags` vocabulary as raw `(tag_id, tag)` pairs.

        Pairs rather than `GenomeTag`s, and a seeder rather than
        `BulkCatalogRepository.replace_genome_tags`, because the cases below
        need to write vocabularies that method refuses outright -- a gap being
        the point of one of them. Seeding through the shipped writer would
        make the refusal untestable and would couple this suite to a second
        port, which is the same reason `vector` above is not `upsert_genome_
        vectors`.
        """


class GenomeRepositoryContract:
    """Subclasses supply a `repository` and a `seeder` fixture.

    Not an `ABC`, matching every other contract suite here: the fixtures are
    supplied by pytest rather than by inheritance, so `@abstractmethod` has
    nothing to attach to and an `ABC` with no abstract members is a lie about
    what enforces the shape. What enforces it is that a subclass without the
    fixtures errors at collection.
    """

    async def test_a_title_with_no_genome_row_reads_as_none_not_a_zero_vector(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """ADR-0014 at the 20th site in `src/` (counted with
        `grep -rl 'ADR-0014' src/`, not trusted). A zero vector is not an
        absence: it is a specific vector at cosine 0.0 from everything, so
        the 98.7% of the catalog with no genome row would score as maximally
        dissimilar from every candidate, silently, while every gauge read
        healthy. Kills `return GenomeVectorRow(title_id, (0.0,) * 1128, rev)`
        and any other padding of the missing case.
        """
        title_id = await seeder.title()
        assert await repository.get(title_id) is None

    async def test_the_vector_reads_back_at_full_width_and_in_order(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """`halfvec` is lossy by design (M6 measured max cosine error
        1.21e-04) but it is not lossy in *width* or in *order*. Kills a
        round-trip that truncates, and one that reads the lanes back
        reversed -- which no similarity number would ever reveal, because a
        reversed vector is a perfectly well-formed vector describing a
        different film.

        Asserted with distinct, asymmetric lane values for exactly that
        reason: a palindrome would survive the reversal mutation.
        """
        title_id = await seeder.title()
        stored = lanes(0.125, 0.25, 0.5, 0.75)
        await seeder.vector(title_id, stored)

        row = await repository.get(title_id)

        assert row is not None
        assert row.title_id == title_id
        assert len(row.relevance) == WIDTH
        assert row.relevance == pytest.approx(stored, abs=1e-3)
        assert row.genome_revision == RELEASE_A

    async def test_get_pair_returns_both_vectors_when_both_exist_at_one_release(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """The ordinary path, and the control the three refusal cases below
        need: without it, an implementation that returns `None`
        unconditionally passes every one of them."""
        left, right = await seeder.title(), await seeder.title()
        await seeder.vector(left, lanes(0.5))
        await seeder.vector(right, lanes(0.0, 0.5))

        pair = await repository.get_pair(left, right)

        assert pair is not None
        assert (pair[0].title_id, pair[1].title_id) == (left, right)

    async def test_get_pair_returns_none_when_only_one_side_has_a_vector(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """Kills an implementation that pads the missing side, which is the
        zero-vector defect wearing a different name -- and one that returns
        the single row it found, which would then be blended against itself.
        At 1.29% coverage this is overwhelmingly the common outcome, so it is
        the arm that runs in production."""
        left, right = await seeder.title(), await seeder.title()
        await seeder.vector(left, lanes(0.5))

        assert await repository.get_pair(left, right) is None
        assert await repository.get_pair(right, left) is None

    async def test_get_pair_refuses_two_vectors_from_different_releases(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """Kills an implementation that returns both whenever both exist.

        The tag vocabulary can change between releases, and two vectors from
        different releases are type-identical, same-width and otherwise
        indistinguishable -- so the resulting cosine is wrong and plausible,
        which is exactly the failure mode this milestone opens by naming.
        The state is reachable: a killed re-import against a new upload
        leaves the table half-migrated, which is precisely what a one-row
        "current revision" table could not express and what this column can.
        """
        left, right = await seeder.title(), await seeder.title()
        await seeder.vector(left, lanes(0.5), revision=RELEASE_A)
        await seeder.vector(right, lanes(0.25), revision=RELEASE_B)

        assert await repository.get_pair(left, right) is None
        # Both orders: a guard written as `left.revision == RELEASE_A` rather
        # than as a comparison of the two would pass one way and not the
        # other.
        assert await repository.get_pair(right, left) is None

    async def test_a_catalog_with_no_vocabulary_reads_as_none_rather_than_raising(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """Absence is a value here and a mismatch is an error, and this is the
        value half.

        It is not a hypothetical state: `ffa` shipped `genome_scores` with the
        vocabulary deliberately unstored, so **every catalog bootstrapped
        before `m08b` is in exactly this state** and stays in it until an
        operator re-runs the phase. A caller that gets `None` renders no tags,
        which is PRD 08's "a degraded subsystem narrows functionality; it
        never fails a request local state can answer". Kills an implementation
        that treats an empty table as a mismatch, which would park every job
        that asks on a deployment where nothing is wrong.

        A vector is seeded so the case cannot pass because the whole genome is
        empty -- the two tables are independent and the wrong implementation
        this rules out reads the wrong one.
        """
        title_id = await seeder.title()
        await seeder.vector(title_id, lanes(0.5))

        assert await repository.vocabulary(RELEASE_A) is None

    async def test_the_vocabulary_reads_back_in_lane_order_not_in_storage_order(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """`result[i]` names `relevance[i]`, which is the only thing this
        method is for.

        Seeded **descending** and asserted ascending: storage order and lane
        order are the same on any fixture that seeds in order, so an
        implementation with no `ORDER BY` -- or one that keys by insertion --
        is invisible against the natural fixture. Same family as the UUIDv7
        `ORDER BY` trap, arriving at a table whose key is not a UUID at all.

        The premise, asserted rather than assumed: the seeding order is not
        the lane order, so a read that preserved it would answer differently.
        """
        seeded = [(3, "melancholy"), (2, "atmospheric"), (1, "zeppelins")]
        assert [tag_id for tag_id, _ in seeded] != sorted(tag_id for tag_id, _ in seeded)
        await seeder.tags(seeded)

        assert await repository.vocabulary(RELEASE_A) == ("zeppelins", "atmospheric", "melancholy")

    async def test_the_vocabulary_refuses_a_release_it_was_not_loaded_under(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """The failure this table's third column exists for, and it is worse
        than the sibling one `get_pair` refuses: a cosine taken across two
        releases is a wrong number, and a *label* taken across two releases is
        a sentence about a household's taste, in prose, on a screen, with
        nothing anywhere reporting an error.

        `PortDataMalformed` rather than a `None`: retrying does not help and
        `JobWorker` parks it, which is the response an operator's re-import is
        the fix for. Both revisions are in the message, because "the
        vocabulary is wrong" without saying *which* release is stored is not
        something an operator can act on.

        Kills an implementation that answers whatever is stored, and one that
        answers `None` -- which would be indistinguishable from the
        legitimately-empty state above.
        """
        await seeder.tags([(1, "zeppelins"), (2, "atmospheric")], revision=RELEASE_B)

        with pytest.raises(PortDataMalformed) as exc_info:
            await repository.vocabulary(RELEASE_A)

        assert RELEASE_A in str(exc_info.value)
        assert RELEASE_B in str(exc_info.value)

    async def test_a_vocabulary_with_a_gap_is_refused_rather_than_shifting_every_later_lane(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """A read that collected the names in `tag_id` order and handed them
        back would give lane 2 the name of tag 4 -- and every lane after it
        the name of the tag one further on -- while returning a
        perfectly-shaped tuple of the wrong length.

        `replace_genome_tags` refuses to *write* one, so reaching this needs a
        hand-written `DELETE`; it is three lines in the reader and it is the
        difference between refusing and answering wrongly. Kills
        `tuple(name for _, name in rows)`, which is the obvious spelling.
        """
        await seeder.tags([(1, "zeppelins"), (2, "atmospheric"), (4, "melancholy")])

        with pytest.raises(PortDataMalformed, match="contiguous"):
            await repository.vocabulary(RELEASE_A)

    async def test_get_pair_of_a_title_with_itself_is_not_a_special_case(
        self, repository: GenomeRepository, seeder: GenomeSeeder
    ) -> None:
        """A self-pair is a legitimate call -- it is what a caller does when
        it has not yet excluded the seed from its own candidate list -- and it
        must return the row twice rather than `None`. Kills an implementation
        that spells the two-row read as `WHERE title_id IN (:a, :b)` and then
        checks `len(rows) == 2`, which finds one row for a self-pair and
        reports the vector as missing.
        """
        title_id = await seeder.title()
        await seeder.vector(title_id, lanes(0.5, 0.25))

        pair = await repository.get_pair(title_id, title_id)

        assert pair is not None
        assert pair[0].title_id == pair[1].title_id == title_id
