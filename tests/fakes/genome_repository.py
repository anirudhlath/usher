"""In-memory `GenomeRepository`.

**Where this is more forgiving than Postgres, on purpose.** Three places,
each of which the paired `tests/integration/test_genome_repository.py` run
is what actually closes:

- **No `halfvec` and therefore no quantisation.** A vector round-trips here
  bit-exactly; through `halfvec(1128)` it does not (M6 measured max cosine
  error 1.21e-04 over 1,000 vectors). The contract compares with a tolerance
  for exactly this reason, so the two arms can share one assertion.
- **No width declaration.** `halfvec(WIDTH)` rejects a vector of the wrong
  length at the database; this dict stores whatever it is handed. So "the
  importer verified the vocabulary width" is a property only the real arm
  can fail on, which is why the importer checks it before reading a score
  rather than relying on the column.
- **No foreign key**, so a vector for a title that does not exist is
  storable here and is rejected there. `GenomeSeeder.title()` exists so no
  contract case depends on which.

`titles` is a test-double affordance written only by `FakeGenomeSeeder`; the
port never writes it, and neither will it -- the writer is
`BulkCatalogRepository.upsert_genome_vectors`.
"""

import uuid
from dataclasses import dataclass, field

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
