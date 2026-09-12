"""In-memory `GenomeRepository`."""

import uuid
from dataclasses import dataclass, field

from usher.ports.errors import PortDataMalformed
from usher.ports.repository import GenomeRepository, GenomeVectorRow


@dataclass
class FakeGenomeRepository(GenomeRepository):
    """One dict, keyed on title id.

    `titles` records which ids exist at all, so the fake can distinguish "a
    title with no vector" from "no such title" the way a foreign key does --
    not because any port method reads it, but because a seeder that could not
    make that distinction would let a case pass here for a reason that does
    not hold there.
    """

    vectors: dict[uuid.UUID, GenomeVectorRow] = field(default_factory=dict)
    titles: set[uuid.UUID] = field(default_factory=set)
    #: `tag_id -> (tag, genome_revision)`, written only by `FakeGenomeSeeder`.
    #: Independent of `vectors` because the two tables are, which is the whole
    #: reason `vocabulary` compares a revision instead of joining.
    tags: dict[int, tuple[str, str]] = field(default_factory=dict)

    async def get(self, title_id: uuid.UUID) -> GenomeVectorRow | None:
        return self.vectors.get(title_id)

    async def get_pair(
        self, left: uuid.UUID, right: uuid.UUID
    ) -> tuple[GenomeVectorRow, GenomeVectorRow] | None:
        # Two independent lookups rather than a set membership test: a
        # self-pair is a legitimate call and must return the row twice, which
        # is the mutation `WHERE title_id IN (:a, :b)` plus `len(rows) == 2`
        # fails on.
        first = self.vectors.get(left)
        second = self.vectors.get(right)
        if first is None or second is None:
            return None
        if first.genome_revision != second.genome_revision:
            # Not an error: a mixed table is a real, recoverable state (a
            # killed re-import against a new upload) and the honest answer to
            # "compare these two" is that they are not comparable.
            return None
        return first, second

    async def vocabulary(self, revision: str) -> tuple[str, ...] | None:
        # Sorted by `tag_id` rather than trusting insertion order, which is
        # what the Postgres arm's `ORDER BY` does and what makes the two
        # answer alike for a vocabulary seeded out of order.
        rows = sorted(self.tags.items())
        if not rows:
            return None
        stored = {row_revision for _, (_, row_revision) in rows}
        if stored != {revision}:
            raise PortDataMalformed(
                f"the stored genome vocabulary was loaded from release "
                f"{'/'.join(sorted(stored))} and cannot name the lanes of a vector from "
                f"{revision}; re-run bootstrap --phase movielens",
                detail=revision,
            )
        if [tag_id for tag_id, _ in rows] != list(range(1, len(rows) + 1)):
            raise PortDataMalformed(
                f"the stored genome vocabulary is not contiguous 1...{len(rows)}; a gap "
                "moves every later lane's name",
                detail=revision,
            )
        return tuple(tag for _, (tag, _) in rows)
