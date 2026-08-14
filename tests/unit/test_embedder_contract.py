"""Every `Embedder` in this repository against the shared contract, plus the
properties that belong to one implementation rather than to the port.

Two arms run here. `FakeEmbedder`, which is a hash and touches nothing; and
`OpenAICompatEmbedder`, driven over `httpx.MockTransport` -- no network, no
server and no model, because `tests/unit` has none of the three. The second
arm's transport answers with `tests.fakes.embedding`'s own vector for each
text it is handed, so the bytes on the wire are a model's answer rather than
a fixture literal, and the two arms are two *runtimes* of one construction
rather than two fakes.

**`FastEmbedEmbedder` does not sign this contract, and this docstring claimed
it did until 2026-08-13.** The sentence read *"the real `FastEmbedEmbedder`
runs the identical suite behind a marker: it downloads 129-134 MB and loads a
model in 4.84 s cold, so it is opt-in"*, which describes a plausible
arrangement that has never existed. Checked rather than re-asserted:
`EmbedderContract` had exactly **one** subclass before the arm below was added
and has two now, and `pyproject.toml` registers exactly one marker
(`integration`) under `--strict-markers`, so no other marker could even be
spelled. The download cost is real and is why nobody built the arm; the arm is
the part that was invented.

**What that costs, stated because a false claim of coverage is worse than an
admitted gap:** the port's own contract has never run against a real model, so
every property in it -- unit norms, width, batch order, same-text stability --
is asserted only against constructions this repository controls. The one
property that genuinely needs the real checkpoint is covered elsewhere and
deliberately: `FastEmbedEmbedder` asserts the norm on its **first batch at
runtime**, precisely because a checkpoint that loses its `Normalize` module
returns norms of 8.99-9.46 and no test running against a stand-in can see it.
That runtime assertion is the coverage; this file is not.
"""

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


# --------------------------------------------------------------------------
# The HTTP arm

#: **Deliberately not `EMBEDDING_DIMENSIONS`, and the same 8 the sibling unit
#: file uses.** `Embedder.dimension` is the *model's* width and
#: `composition.embedder` narrows a deployment whose model disagrees with the
#: stored column, so an arm that took its width from that constant would make
#: the comparison `x == x` and this class would be what ratified it. Eight is
#: enough to make a norm and a width two different facts.
_DIMENSION = 8

#: `.invalid` is reserved by RFC 6761 and can never resolve, so a case that
#: escaped the mock transport fails rather than talking to somebody.
_BASE_URL = "https://embeddings.invalid/v1"

_CHECKPOINT = "BAAI/bge-m3"
_MODEL_NAME = f"{RUNTIME}:{_CHECKPOINT}"


class _ServedModel:
    """The mock transport's handler: a stand-in for a served model.

    **It computes an answer per text rather than replaying one, and the
    difference is measured rather than argued.** A constant vector satisfies
    "every vector is unit normalised" and "every vector is the declared width"
    for every batch there will ever be, and it is deterministic by
    construction -- **measured 2026-08-13 by wiring one: four of the five
    inherited cases still pass**, and only
    `test_a_batch_returns_one_vector_per_input_in_order` fails. That case is
    the one a constant cannot reach, because it asks for `batch[0] !=
    batch[1]`, for `batch[0] == batch[2]` over a repeated text, and for a
    batch's vectors to equal what single-text calls return -- all three of
    which are true of a function of the *text* and of nothing else. A handler
    keyed on **position** rather than on text scores identically (4 of 5, same
    case failing), which is the second shape this construction rules out.

    **`_vector` is imported by its private name on purpose.** It is the
    construction `FakeEmbedder` answers with (`blake2b` -> Box-Muller ->
    L2-normalise, non-vacuity measured in that module's docstring), and
    reusing it is what makes this transport a second runtime of one model
    rather than a second fake. The public door, `FakeEmbedder.embed`, is
    `async`, and an `httpx.MockTransport` handler is called synchronously
    from inside the running loop; the only alternative to the import is a
    second copy of the construction here.

    **The response is served rotated by one position, so the protocol's
    `index` is load-bearing.** An endpoint is free to reorder -- the schema
    carries an `index` on every embedding object precisely because arrival
    order is not promised -- and against a *tidy* transport the ordering case
    would pass for an implementation that never read `index` at all.
    Measured, with an `OpenAICompatEmbedder` subclass that returns the
    objects in arrival order: it fails
    `test_a_batch_returns_one_vector_per_input_in_order` against this handler
    and **passes** it against the same handler with the rotation removed.

    A *rotation* and not a reversal, which is the identity-element family in
    `.claude/rules/testing-discipline.md` arriving at a permutation: the
    contract's batch holds the same text at positions 0 and 2, so it is
    invariant under a reversal and an arrival-order implementation would
    answer it correctly by accident. A 3-cycle is the smallest permutation
    that is not its own inverse.
    """

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
    """The same five cases against the adapter that talks to a server.

    **Three divergences, because what this arm can say is narrower than what
    it looks like it says.** Written down the way
    `.claude/rules/fixtures-and-fakes.md` writes down every difference between
    a fake and its Postgres arm, rather than left to be inferred from the
    fixture. **Nothing is skipped and nothing is overridden**: all five cases
    run, and the two below them are this arm's own.

    - **No case here is a statement about `bge-m3`.** The norm and the width
      are the stand-in's, so `test_every_vector_is_unit_normalised` pins that
      the adapter hands a good vector through unchanged -- not that the served
      checkpoint normalises, which for a model in somebody else's process is
      the thing the adapter *checks* rather than provides. The cases with
      teeth on that are `test_a_vector_that_is_not_unit_normalised_is_refused`
      and `test_a_vector_of_the_wrong_width_is_refused` in
      `test_adapters_embedding_openai_compat.py`, where the right answer is a
      `PortDataMalformed` rather than a satisfied contract, and this arm can
      never reach them: an adapter under test that refuses is an arm that
      errors.
    - **The width case has one constant on both sides and cannot have two.**
      `_DIMENSION` is what the adapter is told and what the transport serves.
      That is forced rather than lazy -- `_check_first` refuses a first batch
      whose width disagrees with the declared one, so an arm feeding two
      different numbers raises before any assertion is reached. What the case
      still kills here is an adapter that truncated, padded or re-cast what it
      was served; what it cannot express is a disagreement between the model
      and the deployment, which is `composition.embedder`'s comparison and is
      why the constant above is not `EMBEDDING_DIMENSIONS`.
    - **Determinism belongs to the served model, not to `src/`.** Two
      identical calls are two round trips and this transport answers both the
      same way by construction, so the same-text case cannot fail for a reason
      a real endpoint could produce -- a
      non-deterministic kernel on the far side is invisible from here, and
      `test_the_fake_is_deterministic_across_processes` above has no analogue
      for a model this process does not run. What it does pin is the half this
      project owns: that the adapter keeps no per-call state that reaches the
      answer.
    """

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
        """**A contract arm that exercises nothing reads as coverage in the
        summary line**, and two ways for this one to do so are cheap to close.

        The five cases above are inherited, so not one of them names
        `OpenAICompatEmbedder`: a fixture handing back a `FakeEmbedder` would
        make this class a slower copy of `TestFakeEmbedder`, and a fixture
        whose transport nothing reaches would make it a copy that also proves
        the transport is decorative. Both are asserted, the second on the
        request the mock really received.

        The last assertion is about the deployment rather than about this
        file. `Embedder.dimension` is the model's own width and
        `composition.embedder` declines to claim index jobs when it disagrees
        with `EMBEDDING_DIMENSIONS`; an arm that took the adapter's width from
        that constant would turn the comparison into `x == x`, and the port's
        docstring names exactly that temptation.
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
        """**The premises the five inherited cases rest on, read off the bytes
        the transport really served** rather than off the literal the handler
        was built from.

        Four claims, and without each of them some case above is satisfied by
        an implementation doing something else: the response arrived in an
        order that is not the input's, so `index` is what the answer was
        rebuilt from; that order is not its own inverse, because the contract's
        own batch carries one text at positions 0 and 2 and is therefore
        invariant under a reversal -- the shape
        `.claude/rules/testing-discipline.md` records as a permutation a
        3-cycle is the smallest escape from; arrival order and the answer are
        different lists; and the three vectors differ from one another, so
        nothing above is being satisfied by a constant.
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
