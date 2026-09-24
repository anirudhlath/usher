"""Every `Embedder` here against the contract, plus per-implementation properties."""

import json
import math
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.contract.embedder_contract import EmbedderContract
from tests.fakes.embedding import FakeEmbedder, planted_pair
from tests.fakes.embedding import _vector as _model_vector
from usher.adapters.embedding.openai_compat import RUNTIME, OpenAICompatEmbedder
from usher.db.models.search import EMBEDDING_DIMENSIONS
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
    """The one case that would catch `hash()` in place of `hashlib`.

    `str.__hash__` is salted by `PYTHONHASHSEED`, so an
    `np.random.default_rng(abs(hash(text)))` spelling passes every case in
    `EmbedderContract` and fails only here: two interpreters, two seeds, one
    expected answer. The environment is the running one with `PYTHONHASHSEED`
    overridden, so the child resolves the same venv, and `check=True` keeps a
    subprocess that failed to import from reading as a pass.
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
    """A helper nothing checks is a helper that drifts.

    `planted_pair` is what lets a similarity test state a number, so its angle
    and its norm are both pinned here.
    """
    first, planted = planted_pair(theta)
    cosine = sum(one * other for one, other in zip(first, planted, strict=True))
    assert cosine == pytest.approx(math.cos(theta), abs=1e-12)
    norm = math.sqrt(sum(value * value for value in planted))
    assert norm == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------------
# The HTTP arm

#: Deliberately not `EMBEDDING_DIMENSIONS`. `Embedder.dimension` is the
#: *model's* width, and an arm taking it from that constant would turn the
#: comparison below into `x == x`.
_DIMENSION = 8

#: `.invalid` is reserved by RFC 6761 and can never resolve, so a case that
#: escaped the mock transport fails rather than talking to somebody.
_BASE_URL = "https://embeddings.invalid/v1"

_CHECKPOINT = "BAAI/bge-m3"
_MODEL_NAME = f"{RUNTIME}:{_CHECKPOINT}"


class _ServedModel:
    """The mock transport's handler: a stand-in for a served model."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension
        #: The `input` array of every request that crossed the transport. Read
        #: by `model_calls`, so the contract's empty-batch case asserts on a
        #: round trip that did not happen rather than on a result that
        #: happened to be empty.
        self.requests: list[list[str]] = []
        #: The `data` array of every response, so a premise about the order
        #: the bytes arrived in is read off what was really served.
        self.data: list[list[dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        texts = [str(text) for text in json.loads(request.content)["input"]]
        self.requests.append(texts)
        rotated = [*range(1, len(texts)), 0] if texts else []
        data: list[dict[str, Any]] = [
            {
                "object": "embedding",
                "index": index,
                "embedding": _model_vector(texts[index], self._dimension),
            }
            for index in rotated
        ]
        self.data.append(data)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": _CHECKPOINT,
                "data": data,
                "usage": {"prompt_tokens": 11, "total_tokens": 11},
            },
        )


class TestOpenAICompatEmbedder(EmbedderContract):
    """The same five cases against the adapter that talks to a server."""

    endpoint: _ServedModel

    @pytest_asyncio.fixture
    async def embedder(self) -> AsyncIterator[OpenAICompatEmbedder]:
        self.endpoint = _ServedModel(_DIMENSION)
        built = OpenAICompatEmbedder(
            _MODEL_NAME,
            base_url=_BASE_URL,
            dimension=_DIMENSION,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.endpoint)),
        )
        try:
            yield built
        finally:
            # The release callable `composition.embedder` hands every entry
            # point's `finally`, exercised once per case rather than left to
            # the garbage collector.
            await built.aclose()

    def model_calls(self, embedder: Embedder) -> int | None:
        """A round trip, which is what "a call to the model" means here.

        Stronger than the fake's counter rather than weaker: on a metered
        endpoint the empty batch the contract refuses to send is a billed
        request, and on any endpoint it is a round trip for nothing.
        """
        assert isinstance(embedder, OpenAICompatEmbedder)
        return len(self.endpoint.requests)

    async def test_this_arm_drives_the_shipped_adapter_over_the_transport(
        self, embedder: Embedder
    ) -> None:
        """A contract arm that exercises nothing still reads as coverage.

        The five cases above are inherited, so not one of them names
        `OpenAICompatEmbedder`: a fixture handing back a `FakeEmbedder`, or one
        whose transport nothing reaches, would leave this class decorative.
        Both are closed here, the second on the request the mock really
        received. The last assertion keeps this arm's width distinct from
        `EMBEDDING_DIMENSIONS`, which would otherwise make it `x == x`.
        """
        assert isinstance(embedder, OpenAICompatEmbedder)
        assert embedder.model_name == _MODEL_NAME

        vectors = await embedder.embed(["a caretaker inventories a house"])

        assert self.endpoint.requests == [["a caretaker inventories a house"]], (
            "the embedder answered without crossing the transport"
        )
        assert len(vectors[0]) == _DIMENSION
        assert embedder.dimension == _DIMENSION
        assert _DIMENSION != EMBEDDING_DIMENSIONS, (
            "the premise: this arm's width is the schema's, so a wrong-width "
            "model would agree with the column by construction"
        )

    async def test_the_endpoint_answers_per_text_and_out_of_arrival_order(
        self, embedder: Embedder
    ) -> None:
        """The premises the inherited cases rest on, read off the served bytes.

        Four claims, each of which some case above would otherwise satisfy by
        accident: the response arrived in an order that is not the input's, so
        `index` is what the answer was rebuilt from; that order is not its own
        inverse, which the contract's duplicate-carrying batch cannot tell from
        no sort at all; arrival order and the answer are different lists; and
        the three vectors differ, so nothing above is satisfied by a constant.
        """
        texts = ["a caretaker inventories a house", "harbour lights", "vane"]

        vectors = await embedder.embed(texts)

        arrived = [entry["index"] for entry in self.endpoint.data[0]]
        assert arrived != sorted(arrived), "the premise: the transport served input order"
        assert [arrived[index] for index in arrived] != sorted(arrived), (
            "the premise: the served permutation is its own inverse, which the "
            "contract's duplicate-carrying batch cannot tell from no sort at all"
        )
        assert [entry["embedding"] for entry in self.endpoint.data[0]] != vectors, (
            "the premise: arrival order and the sorted answer are the same list"
        )
        assert vectors == [_model_vector(text, _DIMENSION) for text in texts]
        assert len({tuple(vector) for vector in vectors}) == len(texts), (
            "the premise: the endpoint answers per text rather than a constant"
        )
