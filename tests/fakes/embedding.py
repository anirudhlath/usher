"""Deterministic `Embedder`: blake2b -> Box-Muller -> L2-normalise."""

import hashlib
import math
from collections.abc import Sequence

from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.ports.embedding import Embedder

# Tracks the storage column rather than restating its width as a second literal.
_DIMENSION = EMBEDDING_DIMENSIONS
_DIGEST_BYTES = 64
_WORDS_PER_DIGEST = _DIGEST_BYTES // 8


class FakeEmbedder(Embedder):
    def __init__(self, *, dimension: int = _DIMENSION, model_name: str | None = None) -> None:
        self._dimension = dimension
        self._model_name = model_name or f"fake:blake2b-box-muller-{dimension}"
        # Read by `EmbedderContract.model_calls`, so the empty-batch case
        # asserts on a call that did not happen rather than on a result that
        # happened to be empty.
        self.calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            # Before `self.calls` is touched: an empty batch is not a call.
            return []
        self.calls.append(list(texts))
        return [_vector(text, self._dimension) for text in texts]

    async def aclose(self) -> None:
        return None


def _uniforms(text: str, count: int) -> list[float]:
    """`count` values in (0, 1], from blake2b over `text` and a counter.

    Blake2b rather than a PRNG seeded from `hash(text)`: `str.__hash__` is
    salted by `PYTHONHASHSEED`, so the seeded form passes every in-process
    case and still yields different vectors in a worker process than in the
    test process, which would make `source_fingerprint` staleness meaningless.
    """
    values: list[float] = []
    counter = 0
    while len(values) < count:
        digest = hashlib.blake2b(
            text.encode("utf-8") + counter.to_bytes(8, "big"), digest_size=_DIGEST_BYTES
        ).digest()
        for word in range(_WORDS_PER_DIGEST):
            raw = int.from_bytes(digest[word * 8 : word * 8 + 8], "big")
            # (raw + 1) / 2**64 lands in (0, 1] -- never exactly 0, so the
            # log() below is always defined.
            values.append((raw + 1) / 2.0**64)
        counter += 1
    return values[:count]


def _vector(text: str, dimension: int) -> list[float]:
    uniforms = _uniforms(text, dimension + dimension % 2)
    gaussians: list[float] = []
    for index in range(0, len(uniforms), 2):
        # Box-Muller: two uniforms in, two independent standard normals out.
        radius = math.sqrt(-2.0 * math.log(uniforms[index]))
        angle = 2.0 * math.pi * uniforms[index + 1]
        gaussians.append(radius * math.cos(angle))
        gaussians.append(radius * math.sin(angle))
    gaussians = gaussians[:dimension]
    # A gaussian vector normalised is uniform on the sphere. The norm is zero
    # only with probability ~2**-64 per component; not defended, and named
    # here so nobody mistakes its absence for an oversight.
    norm = math.sqrt(sum(value * value for value in gaussians))
    return [value / norm for value in gaussians]


def planted_pair(theta: float, *, dimension: int = _DIMENSION) -> tuple[list[float], list[float]]:
    """Two unit vectors at exactly `theta` radians, for tests that need a *known* similarity.

    `v = cos(theta)*a + sin(theta)*b` with `a` and `b` orthonormal, so
    `dot(a, v) == cos(theta)` exactly. A test that needs "these two are 0.9
    similar" states 0.9 and gets it, rather than hoping a hash lands on the
    right side of a threshold.
    """
    first = [0.0] * dimension
    first[0] = 1.0
    second = [0.0] * dimension
    second[1] = 1.0
    planted = [
        math.cos(theta) * one + math.sin(theta) * other
        for one, other in zip(first, second, strict=True)
    ]
    return first, planted
