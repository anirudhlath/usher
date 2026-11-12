"""Seed rows -- the port a "Because you watched" row is stored and recalled through."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.seed_rows import SeedRow

__all__ = [
    "SeedRowRepository",
]


class SeedRowRepository(ABC):
    """`seed_rows` -- one record per seed title, the same for every household.

    Methods flush and never commit.
    """

    @abstractmethod
    async def list_for(self, seed_title_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SeedRow]:
        """The records of these seeds, keyed by seed. A seed with no record is absent.

        An empty `seed_title_ids` sends nothing to the store.
        """

    @abstractmethod
    async def save(self, row: SeedRow) -> None:
        """Replace this seed's record with `row`, whole.

        Nothing of the previous record survives that `row` does not carry, so a caller
        keeping the last good cards across a failed attempt carries them into `row`.
        A `seed_title_id` naming no title raises `RepositoryConflict`.
        """
