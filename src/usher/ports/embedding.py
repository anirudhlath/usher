"""Port for computing text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Turns text into vectors. Implementations are expected to batch.

    Contract: vectors are L2-normalised, so downstream cosine similarity
    can be computed as a plain dot product (PRD 05 promises "brute-force
    exact cosine", which is only equivalent to a dot product when inputs
    are unit-normalised). Callers are responsible for any query-side
    instruction prefix their chosen model needs before calling `embed` —
    this port has no query/document distinction, so it cannot apply one
    itself.

    🔶 Provisional — whether that split is the right one (as opposed to,
    say, separate `embed_query`/`embed_documents` methods) is undecided.
    BGE-family models (PRD 05 names `bge-small-en-v1.5`) document a
    query-side instruction prefix that this contract currently pushes
    entirely onto the caller. Settle in M6.
    """

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

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources (e.g. a GPU-resident model)."""
