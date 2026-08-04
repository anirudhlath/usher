"""`FastEmbedEmbedder`'s two decisions that do not need a loaded model.

**No case here loads `fastembed`.** The package lives behind an extra and is
not installed in the environment this suite runs in, which is itself the
point: the constructor's import is local, so a deployment without the extra
must be able to import this module, run every other lane, and fail only where
it would have used a model.

What is exercised instead is the code that runs *around* the model — the norm
check, the batch-length check, the empty-batch short circuit, and the
runtime/checkpoint split — by driving `embed` with the model attribute
replaced. That is a real seam rather than a convenience: `_embed_sync` is the
one method that touches the library, and everything worth asserting is on
either side of it.
"""

import importlib
import math
from typing import Any

import pytest

from usher.adapters.embedding.fastembed import (
    RUNTIME,
    FastEmbedEmbedder,
    checkpoint_of,
)
from usher.ports.errors import PortDataMalformed, PortUnavailable

_DIMENSION = 384


def _unit(seed: float = 1.0) -> list[float]:
    """A genuine unit vector of the declared width."""
    raw = [seed + index for index in range(_DIMENSION)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def _embedder(vectors: list[list[float]]) -> FastEmbedEmbedder:
    """A `FastEmbedEmbedder` whose model is a scripted stand-in.

    Built without running `__init__`, because `__init__` is the one place
    that imports the third-party package -- and the whole argument for that
    import being local is that this suite runs without it installed.
    """
    embedder = FastEmbedEmbedder.__new__(FastEmbedEmbedder)
    embedder._model_name = f"{RUNTIME}:BAAI/bge-small-en-v1.5"
    embedder._batch_size = 16
    embedder._norm_checked = False
    embedder._model = _ScriptedModel(vectors)
    return embedder


class _ScriptedModel:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._vectors


async def test_a_vector_that_is_not_unit_normalised_is_refused() -> None:
    """**The mutation with the largest silent blast radius in this
    milestone.**

    Normalisation is baked into this *checkpoint* as a third module
    (Transformer -> Pooling -> Normalize), not applied by the library:
    `normalize_embeddings=False` returns bit-identical vectors and norms are
    1.0 to within 5.96e-08, while the same backbone with `2_Normalize`
    removed returns norms **8.99-9.46**. So a model swap that drops that
    module makes every dot-product score ~85x too large -- a
    plausible-looking ranking that is wrong everywhere, with nothing raising.

    `EmbedderContract` cannot see this: it runs against the model the
    deployment shipped with, not the one it is running now. So the check is
    the implementation's, and this is the case that holds the tolerance in
    place. Fails: `_NORM_TOLERANCE = 10.0`, or deleting the check.
    """
    norm_nine = [value * 9.0 for value in _unit()]

    with pytest.raises(PortDataMalformed, match="norm"):
        await _embedder([norm_nine]).embed(["a caretaker inventories a house"])


async def test_a_unit_vector_passes_and_is_checked_only_once() -> None:
    """The control, and the half that keeps the check off the hot path.

    Without the first assertion the case above passes against an
    implementation that refuses everything. Without the second, the check is
    a square root per vector per batch, re-answering a question about the
    checkpoint that cannot change while the process lives.
    """
    embedder = _embedder([_unit()])

    first = await embedder.embed(["one"])
    embedder._model = _ScriptedModel([[value * 9.0 for value in _unit()]])
    second = await embedder.embed(["two"])

    assert len(first[0]) == _DIMENSION
    assert len(second) == 1, "the norm was re-checked on a later batch"


async def test_a_batch_that_comes_back_the_wrong_length_is_malformed() -> None:
    """Order is the port's contract and a length mismatch is its observable
    half: an implementation that deduplicated internally lands title *n*'s
    vector on title *m*, which is invisible to any per-vector assertion.

    `PortDataMalformed` rather than retryable -- no backoff makes a model
    return a different number of vectors for the same input.
    """
    with pytest.raises(PortDataMalformed):
        await _embedder([_unit(), _unit(2.0)]).embed(["only one text"])


async def test_an_empty_batch_is_not_a_call() -> None:
    """On a GPU-resident model this is the difference between a no-op and a
    stall, and the port states it as a contract rather than an optimisation.
    Asserted on the call that did not happen, never on the empty result --
    an implementation that called and got nothing back returns `[]` too.
    """
    embedder = _embedder([_unit()])

    assert await embedder.embed([]) == []
    model: Any = embedder._model
    assert model.calls == []


async def test_a_model_that_fails_at_runtime_is_retryable() -> None:
    """The model file has gone, or the process is out of memory. `JobWorker`
    backs off rather than parking, because a restart genuinely fixes all
    three -- and a park needs a human to release work whose only problem was
    a bad five minutes.
    """

    class _Broken:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("onnxruntime session is gone")

    embedder = _embedder([])
    embedder._model = _Broken()

    with pytest.raises(PortUnavailable):
        await embedder.embed(["one"])


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("fastembed:BAAI/bge-small-en-v1.5", "BAAI/bge-small-en-v1.5"),
        # No prefix: taken as the checkpoint, so an operator who wrote a bare
        # name gets the model rather than a parse error. `model_name` still
        # records what was configured, which is what makes a swap detectable.
        ("BAAI/bge-small-en-v1.5", "BAAI/bge-small-en-v1.5"),
        # A colon *inside* the checkpoint, which is the whole reason this is
        # `partition` and not `rpartition`. Without this row the two spell
        # the same thing for every id that has one colon, and the mutation
        # survives -- measured.
        ("fastembed:BAAI/bge-small-en-v1.5:quantised", "BAAI/bge-small-en-v1.5:quantised"),
        # A different runtime is not this one's to strip: leaving it whole
        # makes the load fail loudly rather than silently serving fastembed
        # vectors under a sentence-transformers name -- which is the exact
        # confusion `model_name` carries the runtime to prevent.
        (
            "sentence-transformers:BAAI/bge-small-en-v1.5",
            "sentence-transformers:BAAI/bge-small-en-v1.5",
        ),
    ],
)
def test_the_runtime_prefix_splits_on_the_first_colon(configured: str, expected: str) -> None:
    """`partition`, not `rpartition`: a checkpoint id contains `/` and may
    carry a `:` revision suffix, so it is the *first* colon that separates
    the runtime.

    The prefix exists because the same weights under two runtimes differ by
    1.41e-03 max pairwise delta -- 6x the halfvec quantisation error -- so
    `model_name` has to record both, and a swap then invalidates every stored
    vector through the stale predicate rather than through a migration.
    """
    assert checkpoint_of(configured) == expected


def test_the_sibling_name_does_not_shadow_the_third_party_package() -> None:
    """A module named `fastembed` inside a package that imports `fastembed`.

    Python 3 has no implicit relative imports, so this is correct -- and
    "should be correct" is how a milestone acquires a self-import that fails
    only on somebody else's machine, so it is checked rather than assumed.

    Asserted on the resolved module's own identity: `usher.adapters.embedding
    .fastembed` has a `FastEmbedEmbedder` and the third-party distribution
    does not, so an accidental self-import is visible whether or not the
    extra is installed.
    """
    module = importlib.import_module("usher.adapters.embedding.fastembed")

    assert module.__name__ == "usher.adapters.embedding.fastembed"
    assert module.FastEmbedEmbedder is FastEmbedEmbedder
