"""Curated rows -- the port an LLM generation is persisted through."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.curation import CuratedRow

__all__ = [
    "CuratedRowRepository",
]


class CuratedRowRepository(ABC):
    """`curated_rows` -- what one generation proposed, per household."""

    @abstractmethod
    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        """Replace this user's whole screen with `rows`, atomically."""

    @abstractmethod
    async def list_for_user(self, user_id: uuid.UUID) -> list[CuratedRow]:
        """This user's newest generation, in the model's own order."""
