"""`FakeEmbedder` against the shared `Embedder` contract, plus the two
properties that belong to this class rather than to the port.

The real `FastEmbedEmbedder` runs the identical suite behind a marker: it
downloads 129-134 MB and loads a model in 4.84 s cold, so it is opt-in and
never part of a default selection.
"""

import math
import os
import subprocess
import sys

import pytest

from tests.contract.embedder_contract import EmbedderContract
from tests.fakes.embedding import FakeEmbedder, planted_pair
from usher.ports.embedding import Embedder

_PROBE = (
    "from tests.fakes.embedding import FakeEmbedder;"
    "import asyncio;"
    "print(asyncio.run(FakeEmbedder().embed(['The Quiet Vacuum']))[0][:4])"
)


class TestFakeEmbedder(EmbedderContract):
    @pytest.fixture
    def embedder(self) -> FakeEmbedder:
        return FakeEmbedder()

    def model_calls(self, embedder: Embedder) -> int | None:
        assert isinstance(embedder, FakeEmbedder)
        return len(embedder.calls)


def test_the_fake_is_deterministic_across_processes() -> None:
    """**The one case that would catch `hash()` in place of `hashlib`.**

    The `np.random.default_rng(abs(hash(text)))` spelling passes every case
    in `EmbedderContract` -- norms, width, batch order, same-text
    determinism, empty batch -- and fails only here, because `str.__hash__`
    is salted by `PYTHONHASHSEED`. A worker process and a test process would
    then disagree about a title's vector while every in-process assertion
    stayed green, which ratifies a `source_fingerprint` scheme that does not
    hold.

    Two interpreters, two different seeds, one expected answer.

    The environment is the running one with `PYTHONHASHSEED` overridden,
    not a hand-built minimal dict: under `uv run` the child needs the same
    `VIRTUAL_ENV`/`PYTHONPATH` resolution the parent had, and a subprocess
    that failed to import is DID-NOT-RUN rather than a pass. `check=True`
    is what turns that into an error instead of an empty string compared
    against an empty string.
    """
    outputs = set()
    for seed in ("0", "1"):
        # S603: a fixed argv built from `sys.executable` and a literal, no
        # shell and no external input. The alternative -- asserting
        # determinism inside one process -- is exactly what cannot see this.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"the fake is PYTHONHASHSEED-dependent: {outputs}"
    assert outputs != {""}, "the probe printed nothing; this run proved nothing"


@pytest.mark.parametrize("theta", [0.0, math.pi / 6, math.pi / 3, math.pi / 2])
def test_a_planted_angle_is_exact(theta: float) -> None:
    """A helper nothing checks is a helper that drifts, and this one is the
    reason similarity tests in this milestone are allowed to state a number.
    Measured exact to 2.22e-16 -- one ulp -- so the tolerance below is
    generous by four orders of magnitude and still fails anything that
    stopped being orthonormal."""
    first, planted = planted_pair(theta)
    cosine = sum(one * other for one, other in zip(first, planted, strict=True))
    assert cosine == pytest.approx(math.cos(theta), abs=1e-12)
    norm = math.sqrt(sum(value * value for value in planted))
    assert norm == pytest.approx(1.0, abs=1e-12)
