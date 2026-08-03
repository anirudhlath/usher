"""`Embedder` over `fastembed`, and the norm check that is not decorative.

**A module named `fastembed` inside a package that imports `fastembed`, and
that is safe -- verified rather than assumed.** Python 3 has no implicit
relative imports, so `import fastembed` below resolves to the third-party
distribution and never to this module; the sibling name would only shadow
under Python 2 semantics. Checked directly by
`test_the_sibling_name_does_not_shadow_the_third_party_package`, because "it
should be fine" is how a milestone acquires a self-import that only fails on
somebody else's machine.

**Why `fastembed` and not `sentence-transformers`, which PRD 05 names.**
Measured 2026-08-02 on this host: sentence-transformers is 59 packages,
**2.62 GiB downloaded and 4.8 GiB installed**, against a current `usher`
image of 332 MB -- and **~4.5 GiB of that 4.8 is GPU runtime** (`nvidia/`
2.7 G, `torch/` 1.1 G, `triton/` 689 M) pulled unconditionally on a host that
may never have a GPU. `fastembed` is 28 packages, **167 MiB**, no torch, and
is *faster* on identical input (252.9 texts/s against 229.5). Agreement over
205 documents: min cosine **0.99999619**, top-1 identical 205/205. PRD 05 is
corrected rather than followed.

**Two supply-chain facts that belong in the open.** fastembed serves an
optimised ONNX conversion from a *third-party* repository
(`qdrant/bge-small-en-v1.5-onnx-q`), not BAAI's own weights. And the
ST-vs-fastembed vector difference (max pairwise-similarity delta 1.41e-03) is
**6x the halfvec quantisation error**, so the two runtimes are not
interchangeable without a re-embed -- which is exactly why `model_name`
records the runtime as well as the checkpoint. Swapping the implementation
then invalidates every stored vector through the stale predicate, with no
migration to write.
"""

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

# Comfortably above the measured 5.96e-08 and comfortably below the 8.99 a
# missing `2_Normalize` module produces, so the check cannot be tripped by
# float noise and cannot be passed by the failure it exists to catch.
_NORM_TOLERANCE = 1e-4

_DIMENSION = 384


def checkpoint_of(model_name: str) -> str:
    """`fastembed:BAAI/bge-small-en-v1.5` -> `BAAI/bge-small-en-v1.5`.

    `partition`, not `rpartition`: a checkpoint id contains `/` and may
    contain `:` in a revision suffix, and it is the *first* colon that
    separates the runtime. A bare name with no prefix is taken as the
    checkpoint, so an operator who wrote one gets the model rather than a
    parse error -- the `model_name` column still records what this
    deployment was configured with, which is what makes a swap detectable.
    """
    runtime, separator, checkpoint = model_name.partition(_SEPARATOR)
    return checkpoint if separator and runtime == RUNTIME else model_name


class FastEmbedEmbedder(Embedder):
    """One loaded model, held for the life of the process.

    Constructed by `usher.composition.embedder` and by nothing else. A model
    is a process-lifetime resource: `build_worker` runs once per worker
    *pass* at a 5 s floor, and a load is 4.84 s cold / 0.13 s warm over 65 MB
    of ONNX, so one built per pass would spend more time loading than
    working, forever, with nothing in the logs saying so.

    `TextEmbedding.embed` is synchronous and CPU-bound, so every call goes
    through `asyncio.to_thread`: run inline it would block the event loop for
    the whole batch, which in the server process is every request and every
    push frame waiting on an embedding.
    """

    def __init__(self, model_name: str, *, batch_size: int = 16) -> None:
        # Imported here rather than at module scope, the way
        # `connect_websocket` imports `websockets`: this dependency lives
        # behind an extra, and `usher.composition` -- which builds this -- is
        # imported by every entry point including `usher bootstrap-status`.
        # A deployment that runs no index lane must not pay for the import
        # and must not fail to start without the package.
        from fastembed import TextEmbedding

        self._model_name = model_name
        self._batch_size = batch_size
        self._model: Any = TextEmbedding(
            model_name=checkpoint_of(model_name), batch_size=batch_size
        )
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
        return _DIMENSION

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
            # The model file has gone, the process is out of memory, the
            # runtime failed. All three are `PortUnavailable`: `JobWorker`
            # backs off rather than parking, and a restart genuinely fixes
            # every one of them. A park would need a human to release work
            # whose only problem was a bad five minutes.
            raise PortUnavailable("the embedding model could not run this batch") from exc
        if len(vectors) != len(batch):
            # Order is the port's contract and a length mismatch is the
            # observable half of breaking it: an implementation that
            # deduplicated internally lands title *n*'s vector on title *m*,
            # which is the most damaging bug available in this milestone and
            # is invisible to any per-vector assertion.
            raise PortDataMalformed(
                f"{self._model_name} returned {len(vectors)} vectors for {len(batch)} texts"
            )
        if not self._norm_checked:
            self._norm_checked = True
            # **Asserted, not taken from the model card**, and the reason is
            # mechanical rather than defensive. Normalisation is baked into
            # this *checkpoint* as a third module (Transformer -> Pooling ->
            # Normalize), not applied by the library:
            # `normalize_embeddings=False` returns bit-identical vectors and
            # norms are 1.0 to within 5.96e-08, while the same backbone with
            # `2_Normalize` removed returns norms 8.99-9.46. So a swap that
            # drops that module silently makes every dot-product score ~85x
            # too large, and `EmbedderContract` cannot see it -- it runs
            # against the model this deployment shipped with, not the one it
            # is running now.
            #
            # **Before the halfvec cast, never after**: post-cast norm drift
            # is 1.21e-04 against 1.19e-07, a 1000x change, so the same check
            # over a stored vector fails on a healthy model.
            #
            # Worth stating alongside it: with the `halfvec_cosine_ops`/`<=>`
            # index PRD 05 specifies, normalisation buys **speed, not
            # correctness** -- `<=>` is normalisation-invariant and `<#>` is
            # not, verified against real pgvector. This check protects the
            # brute-force dot-product path and anything that ever moves to
            # `<#>`.
            norm = math.sqrt(sum(value * value for value in vectors[0]))
            if abs(norm - 1.0) > _NORM_TOLERANCE:
                raise PortDataMalformed(
                    f"{self._model_name} returned a vector of norm {norm:.4f}, not 1.0",
                    detail="this checkpoint's Normalize module is missing or was replaced",
                )
        return vectors

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """The blocking half. `fastembed` yields numpy arrays; the port
        promises `list[float]`, and a caller comparing two vectors with `==`
        must not be handed something whose `==` returns an array."""
        return [[float(value) for value in vector] for vector in self._model.embed(texts)]

    async def aclose(self) -> None:
        """Nothing to release: `fastembed`'s ONNX session has no close, and
        the model is freed with this object. Present because the port
        declares it and because a future GPU-resident implementation will
        have something to do here."""
        return None


__all__ = ["RUNTIME", "FastEmbedEmbedder", "checkpoint_of"]
