"""Port for computing text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Turns text into vectors. Implementations are expected to batch."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Stored alongside vectors so a model change is detectable."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width, must match the database column."""

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input in order."""
