"""Deterministic `Embedder`: blake2b -> Box-Muller -> L2-normalise.

**No test may assert semantic relevance against this class.** It is a hash.
Similarity between two related titles is noise, so a test using it cannot
distinguish a working semantic search from one that returns nothing, and any
unit test that asserts relevance here is a defect in the test rather than a
check on the code. It exists for *plumbing*: order, dimension,
normalisation, determinism, and the fingerprinting the backfill drains on.
Relevance is asserted only where a real model runs.

**Its non-vacuity is measured, not assumed -- at 384, which is no longer the
default.** Over 15,996,000 off-diagonal pairs at `dimension=384`: cosine mean
-0.00001, **sd 0.05102 against a theoretical 1/sqrt(384) = 0.05103** (ratio
1.000), max +0.2549, and **zero pairs above 0.5**. `m09e` moved the default to
`EMBEDDING_DIMENSIONS` (1024) and **that run was not repeated**, so read the
numbers as a property of the construction rather than of today's default: the
mechanism is dimension-independent and a wider vector can only concentrate the
off-diagonal distribution further (theoretical sd 1/sqrt(1024) = 0.03125), so
the measured claim is conservative at the new width rather than unverified in
the direction that would matter. Re-run it before quoting a number.
Near-identical inputs stay orthogonal -- "The Quiet Vacuum" against
the same string with a trailing space is -0.033. Norm error 1.11e-16. A
hashing-trick TF-IDF fake was built and **rejected on this evidence**: its
off-diagonal cosine floor is **+0.723** and it collapses case and
punctuation to 1.00000, which is the vacuous-pass failure mode itself.

**Where this is more forgiving than a real embedder, on purpose. Five
places:**

- **It has no semantics at all**, per the paragraph above.
- **It never downloads, never loads a model, never reads `HF_HUB_OFFLINE`.**
  The real path's 4.84 s cold load, its 129-134 MB of blobs, and the
  huggingface_hub failure where a *populated cache with no network* raises
  `RuntimeError: Cannot send a request, as the client has been closed` are
  all invisible from here.
- **No batch ceiling, no GPU, no OOM, and `aclose()` cannot fail.** Nothing
  here exercises one error path.
- **It is exactly unit-normalised by construction**, so the duty the port
  puts on an implementation -- assert the norm on the first batch, because
  normalisation is a property of the *checkpoint* rather than of embedders
  -- is unexercised. That assertion is pinned against the real model.
- **No tokenizer**, so nothing here is linear in tokens and the measured
  ~8,000-10,700 tokens/s invariant (412.7 texts/s at 19 tokens, 18.7 at 516)
  cannot be observed. Any throughput reasoning against this class is
  reasoning about dictionary operations.
"""

import hashlib
import math
from collections.abc import Sequence

from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.ports.embedding import Embedder

#: **Tracks the column rather than restating it, since `m09e`.** This was a
#: literal `384` while the storage width was also a literal 384, and the two
#: agreed for as long as neither moved. `composition.embedder` now narrows a
#: deployment whose embedder is the wrong width -- so a fake left at 384 does
#: not merely store badly, it makes every case that builds a real `embedder()`
#: get `None` back and assert against a deployment with no model. That is how
#: this line was found: two unit cases, both about `HF_HUB_OFFLINE`.
#:
#: A test that wants a *mismatched* width still passes `dimension=` explicitly,
#: which is the affordance the constructor already had.
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

    **`hashlib`, never `hash()`, and this comment is the reason.** The
    obvious spelling -- seeding a PRNG with `abs(hash(text))` -- passes
    *every* case in `EmbedderContract`: unit norms, correct width, batch
    order, same-text determinism, empty batch. It fails only **across
    processes**, because `str.__hash__` is salted by `PYTHONHASHSEED`, and
    nothing inside a single pytest run can observe that. What it would break
    is the thing this milestone is built on: `source_fingerprint` makes
    staleness a SQL predicate, and a double whose vectors differ between the
    worker process and the test process ratifies a scheme that does not
    work. Pinned by `test_the_fake_is_deterministic_across_processes`.
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
    # A gaussian vector normalised is uniform on the sphere, which is what
    # makes the measured off-diagonal sd match 1/sqrt(384) to three decimal
    # places. The norm is zero only with probability ~2**-64 per component;
    # not defended, and named here so nobody mistakes its absence for an
    # oversight.
    norm = math.sqrt(sum(value * value for value in gaussians))
    return [value / norm for value in gaussians]


def planted_pair(theta: float, *, dimension: int = _DIMENSION) -> tuple[list[float], list[float]]:
    """Two unit vectors at exactly `theta` radians, for tests that need a
    *known* similarity.

    `v = cos(theta)*a + sin(theta)*b` with `a` and `b` orthonormal, so
    `dot(a, v) == cos(theta)` exactly -- verified to 2.22e-16, i.e. one ulp.

    **This exists so that no similarity test has to hope.** The alternative
    is picking a threshold and trusting a hash to land on the right side of
    it, which produces a case that goes red on an unrelated change and gets
    loosened -- once, permanently, and then it asserts nothing. A test that
    needs "these two are 0.9 similar" states 0.9 and gets 0.9.
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
