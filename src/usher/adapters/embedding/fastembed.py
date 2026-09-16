"""`Embedder` over `fastembed`, and the norm check that is not decorative."""

import asyncio
import math
from collections.abc import Sequence
from typing import Any

from usher.ports.embedding import Embedder
from usher.ports.errors import PortDataMalformed, PortUnavailable

# The runtime prefix `model_name` carries, and the separator that splits it
# back off for the library. `fastembed:BAAI/bge-small-en-v1.5` is one string
# to an operator and to the `model_name` column, and two facts here.
RUNTIME = "fastembed"
_SEPARATOR = ":"

# Comfortably above float noise and comfortably below the 8.99 a missing
# `2_Normalize` module produces, so the check cannot be tripped by rounding
# and cannot be passed by the failure it exists to catch.
_NORM_TOLERANCE = 1e-4


def checkpoint_of(model_name: str) -> str:
    """`fastembed:BAAI/bge-small-en-v1.5` -> `BAAI/bge-small-en-v1.5`.

    `partition`, not `rpartition`: a checkpoint id contains `/` and may contain
    `:` in a revision suffix, so it is the *first* colon that separates the
    runtime. A bare name with no prefix is taken as the checkpoint, so an
    operator who wrote one gets the model rather than a parse error.
    """
    runtime, separator, checkpoint = model_name.partition(_SEPARATOR)
    return checkpoint if separator and runtime == RUNTIME else model_name


class FastEmbedEmbedder(Embedder):
    """One loaded model, held for the life of the process.

    Constructed by `usher.composition.embedder` and by nothing else. A model is
    a process-lifetime resource: `build_worker` runs once per worker *pass* at a
    5 s floor, and a cold load costs seconds, so one built per pass would spend
    more time loading than working with nothing in the logs saying so.

    `TextEmbedding.embed` is synchronous and CPU-bound, so every call goes
    through `asyncio.to_thread`: inline it would block the event loop for the
    whole batch -- in the server process, every request waiting on an embedding.
    """

    def __init__(self, model_name: str, *, batch_size: int = 16) -> None:
        # Imported here rather than at module scope: this dependency lives
        # behind an extra, and `usher.composition` -- which builds this -- is
        # imported by every entry point.
        from fastembed import TextEmbedding

        self._model_name = model_name
        self._batch_size = batch_size
        self._model: Any = TextEmbedding(
            model_name=checkpoint_of(model_name), batch_size=batch_size
        )
        # Read off the loaded model rather than declared, so a checkpoint whose
        # width does not match this schema's column is refused by
        # `composition.embedder` at startup instead of by asyncpg, one failed
        # `index` job at a time, in a message about a vector.
        self._dimension = int(self._model.embedding_size)
        # One check, on the first batch, for the reason `embed` gives. A
        # per-batch check would cost a square root per vector on a hot path
        # to re-answer a question about the checkpoint that cannot change
        # while the process lives.
        self._norm_checked = False

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            # Before the model is touched: an empty batch is an empty result
            # and **not a call**. On a GPU-resident model that is the
            # difference between a no-op and a stall.
            return []
        batch = list(texts)
        try:
            vectors = await asyncio.to_thread(self._embed_sync, batch)
        except (OSError, RuntimeError, ValueError) as exc:
            # Model file gone, out of memory, runtime failed: all three are
            # `PortUnavailable`, so `JobWorker` backs off rather than parking
            # work whose only problem was a bad five minutes.
            raise PortUnavailable("the embedding model could not run this batch") from exc
        if len(vectors) != len(batch):
            # Order is the port's contract and a length mismatch is the
            # observable half of breaking it: an implementation that
            # deduplicated internally lands title *n*'s vector on title *m*.
            raise PortDataMalformed(
                f"{self._model_name} returned {len(vectors)} vectors for {len(batch)} texts"
            )
        if not self._norm_checked:
            self._norm_checked = True
            # Asserted rather than trusted: a checkpoint whose normalize module
            # is missing returns vectors cosine distance cannot compare.
            norm = math.sqrt(sum(value * value for value in vectors[0]))
            if abs(norm - 1.0) > _NORM_TOLERANCE:
                raise PortDataMalformed(
                    f"{self._model_name} returned a vector of norm {norm:.4f}, not 1.0",
                    detail="this checkpoint's Normalize module is missing or was replaced",
                )
        return vectors

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """The blocking half.

        `fastembed` yields numpy arrays; the port promises `list[float]`, and a
        caller comparing two vectors with `==` must not get an array back.
        """
        return [[float(value) for value in vector] for vector in self._model.embed(texts)]

    async def aclose(self) -> None:
        """Nothing to release.

        `fastembed`'s ONNX session has no close and the model is freed with this
        object; present because the port declares it.
        """
        return None


__all__ = ["RUNTIME", "FastEmbedEmbedder", "checkpoint_of"]
