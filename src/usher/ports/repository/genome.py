"""The tag genome: one dense vector per title, plus its tag vocabulary."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = [
    "GenomeRepository",
    "GenomeVectorRow",
]


@dataclass(frozen=True, slots=True)
class GenomeVectorRow:
    """One stored genome vector and the release it was computed from."""

    title_id: uuid.UUID
    relevance: tuple[float, ...]
    genome_revision: str


class GenomeRepository(ABC):
    """Read access to the stored MovieLens tag genome.

    The per-title vectors (`genome_scores`), and the vocabulary that names
    their lanes (`genome_tags`).
    """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> GenomeVectorRow | None:
        """The stored vector, or `None` when this title has none.

        `None`, never a zero vector. A zero vector is not "no information":
        it is a specific vector sitting at cosine 0.0 from every other, so a
        title with no genome row would score as maximally dissimilar from
        everything -- an assertion the data never made, made about most of
        the catalog, with every gauge reading healthy while it happened.
        """

    @abstractmethod
    async def get_pair(
        self, left: uuid.UUID, right: uuid.UUID
    ) -> tuple[GenomeVectorRow, GenomeVectorRow] | None:
        """Both vectors, or `None` if either is missing or the two came from different releases.

        The release check is what `genome_revision` exists for: a vector is
        comparable only to another built from the same tags in the same
        order, and two vectors from different releases share a type and a
        width with nothing else to tell them apart. A mixed table yields
        cosines that are wrong and plausible. It is at least countable --
        group `genome_scores` by `genome_revision` -- and a re-import fixes it.

        One call rather than two `get`s because a similarity blend scores a
        pair it already holds. That access pattern is also why there is no
        approximate-nearest-neighbour index over these vectors.
        """

    @abstractmethod
    async def vocabulary(self, revision: str) -> tuple[str, ...] | None:
        """The tag names in lane order for `revision`.

        `result[i]` names `GenomeVectorRow.relevance[i]` — or `None` if no vocabulary is
        stored at all.
        """
