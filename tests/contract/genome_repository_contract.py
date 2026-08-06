"""Behaviour every `GenomeRepository` implementation must satisfy.

Four cases, and three of them are about a value that is *wrong and
plausible* rather than about a value that is missing -- which is what this
milestone's opening section says a genome vector's failures look like. A
zero vector, a padded missing side, and a cosine taken across two releases
all produce a number, in range, with nothing to distinguish it from a right
one.

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

import pytest

from usher.ports.bulk import GENOME_TAG_COUNT
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
