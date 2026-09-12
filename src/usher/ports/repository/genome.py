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
    """Read access to the stored MovieLens tag genome — the per-title vectors
    (`genome_scores`) and the vocabulary that names their lanes (`genome_tags`).
    """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> GenomeVectorRow | None:
        """The stored vector, or `None` when this title has none.

        **`None`, never a zero vector.** ADR-0014 applied to a 1,128-lane
        vector -- the 20th site in `src/`, counted rather than asserted. A
        zero vector is not "no information": it is a specific vector that
        sits at cosine 0.0 from every other vector, so a title with no genome
        row would score as *maximally dissimilar* from everything, which is
        an assertion the data never made, and every gauge would read healthy
        while it happened. At 1.29% coverage that would be 98.7% of the
        catalog.
        """

    @abstractmethod
    async def get_pair(
        self, left: uuid.UUID, right: uuid.UUID
    ) -> tuple[GenomeVectorRow, GenomeVectorRow] | None:
        """Both vectors, or `None` if either is missing **or if the two were
        computed from different releases**.

        The second half is what `genome_revision` exists for: a vector is
        only comparable to another built from the same 1,128 tags in the same
        order, and two vectors from different releases have the same type,
        the same width and nothing else to tell them apart. A mixed table
        then yields cosines that are wrong and plausible, which is the
        failure this milestone opens by naming. A mixed table is also a
        countable condition an operator can see -- `SELECT genome_revision,
        count(*) FROM genome_scores GROUP BY 1` -- with a re-import as the
        fix.

        One call rather than two `get`s because this is the access pattern:
        a similarity blend scores a candidate *pair* it already holds. It is
        also why there is no HNSW index -- see `GenomeScoreRow`.
        """

    @abstractmethod
    async def vocabulary(self, revision: str) -> tuple[str, ...] | None:
        """The tag names in lane order for `revision` — `result[i]` names
        `GenomeVectorRow.relevance[i]` — or `None` if no vocabulary is stored at all.
        """
