"""Port for computing text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Turns text into vectors. Implementations are expected to batch."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Stored alongside vectors, so a model change is one SQL predicate."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width, must match the database column (`halfvec(1024)`).

        **Report the model's own width, never this schema's.** The temptation
        is to return `EMBEDDING_DIMENSIONS`, which makes every implementation
        agree with the column by construction -- and turns
        `composition.embedder`'s startup comparison into `x == x`. The whole
        value of this property is that it can disagree.
        """

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input **in order**.

        Order is the contract, not a convenience: an implementation that
        deduplicates or reorders internally lands title *n*'s vector on
        title *m*, which is the most damaging bug available in this
        milestone and is completely invisible to any per-vector assertion.

        An empty batch is an empty result and **not a call** -- on a
        GPU-resident model that is the difference between a no-op and a
        stall.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources (e.g. a GPU-resident model)."""
