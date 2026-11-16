"""In-memory `SeedRowRepository`.

Divergences from the Postgres arm, recorded rather than modelled:

- There is no `titles` table, so a `seed_title_id` naming no title is stored where
  Postgres raises `RepositoryConflict`. `tests/integration/test_seed_row_repository.py`
  pins that.
- There are no CHECKs. `SeedRow` refuses every record the table would, so the two agree
  on every record the domain can build; a `model_construct`ed one reaches only Postgres.
- It does not skip a save that equals the stored record, since a dict write has no row
  version to spare. The guard is pinned on the Postgres arm through `ctid`.
"""

import uuid
from collections.abc import Sequence

from usher.domain.seed_rows import SeedRow
from usher.ports.repository import SeedRowRepository


class FakeSeedRowRepository(SeedRowRepository):
    def __init__(self) -> None:
        #: The table: one record per seed, whichever household asked.
        self.rows: dict[uuid.UUID, SeedRow] = {}
        #: Calls that reached the store, which is what "no statement" means here. A read
        #: of nothing is answered before it counts.
        self.statements = 0

    async def list_for(self, seed_title_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SeedRow]:
        if not seed_title_ids:
            return {}
        self.statements += 1
        return {
            seed_id: self.rows[seed_id]
            for seed_id in dict.fromkeys(seed_title_ids)
            if seed_id in self.rows
        }

    async def save(self, row: SeedRow) -> None:
        self.statements += 1
        self.rows[row.seed_title_id] = row
