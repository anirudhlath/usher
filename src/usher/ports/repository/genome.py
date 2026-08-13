"""The tag genome: one dense vector per title, plus its tag vocabulary.

Implemented by `usher.db.repositories.genome.PostgresGenomeRepository`.
"""

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
    (`genome_scores`) and the vocabulary that names their lanes
    (`genome_tags`).

    **Read-only, and that is a boundary rather than an omission.** The writers
    are `BulkCatalogRepository.upsert_genome_vectors` and
    `.replace_genome_tags`: both are bulk-import paths, one of them a staged,
    `COPY`-scale, set-based join from `imdb_id` to `titles.id`, which is
    exactly what `BulkCatalogRepository`'s docstring reserves. A `put()` here
    to make test seeding convenient would be a port method nothing in `src/`
    calls, which this project has already shipped once; the contract suite
    seeds through an abstract seeder instead.

    **Two tables on one port because they are one artefact.** A vector whose
    lanes cannot be named and a vocabulary with no vectors to explain are each
    useless alone, and the only invariant either has is a comparison *between*
    them — `genome_scores.genome_revision` against
    `genome_tags.genome_revision`. Splitting them would put the two halves of
    that comparison behind two ports and give a caller a way to hold one
    without the other.

    **Coverage is 1.82% of movies and 1.29% of all titles**, so "this title
    has no vector" is the common case rather than the edge, and every method
    below is written for that.
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
        `GenomeVectorRow.relevance[i]` — or `None` if no vocabulary is stored
        at all.

        **Names positionally, not `(tag_id, tag)` pairs**, because the
        positional alignment with the vector *is* the product. Handing back
        pairs would leave every caller to do the alignment, and a caller that
        indexed by `tag_id` rather than `tag_id - 1` would be off by one on
        every lane with nothing to say so.

        **Absence is a value and a mismatch is an error, and the split is the
        whole design of this method.**

        - `None` when the table is empty. That is a state a real deployment is
          legitimately in: `ffa` shipped vectors with no vocabulary and every
          catalog bootstrapped before `m08b` has exactly this. There is no
          wrong answer to be had from it — a caller renders no tags, which is
          PRD 08's "a degraded subsystem narrows functionality". It is also
          `PortUnavailable`'s own stated distinction ("the requested thing
          does not exist" is a `None` return, see `SourceAdapter.get_item`)
          and `RepositoryNotFound`'s, whose docstring says outright that the
          read-side equivalent of not-found is `None`.
        - **`PortDataMalformed` when a vocabulary is stored under a different
          revision.** Here there *is* a wrong answer available and it is
          plausible: 1,128 names of the right shape, in the right order, for a
          different release, rendered as prose onto a screen. The taxonomy is
          organised by what a caller must do, and this is exactly
          `PortDataMalformed`'s clause — *"retrying does not help, so a caller
          parks the work"*. `JobWorker` parks it immediately rather than
          spending five retries on a table that cannot change on its own; the
          fix is an operator's `usher bootstrap --phase movielens`, which is
          also `get_pair`'s documented fix for the sibling condition.
          `detail` carries both revisions, which is what an operator needs and
          is neither a credential nor a payload.

        **Why not `None` for both, as `get_pair` does?** Because `get_pair`'s
        two outcomes call for the same response and these two do not. There,
        "no vector" and "two releases" both mean *no genome signal*, the
        caller has a documented fallback, and 98.7% of pairs already answer
        `None` — so collapsing them costs nothing. Here they mean "load the
        vocabulary" and "re-import the whole genome", and the second is a
        corrupted table that collapsing would hide behind a state that is
        normal on every pre-`m08b` deployment.

        Also `PortDataMalformed` for a stored vocabulary whose `tag_id`s are
        not `1…n`. The read builds by index for `GenomeVector`'s reason — a
        gap moves every later lane — and `replace_genome_tags` refuses to
        write one, so reaching this needs a hand-written `DELETE`. It is three
        lines and it is the difference between refusing and silently shifting
        every name after the gap.
        """
