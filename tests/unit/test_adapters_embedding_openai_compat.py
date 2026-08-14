"""`OpenAICompatEmbedder` over `httpx.MockTransport`. No network, no model.

**Why this adapter has a unit file at all when `EmbedderContract` exists.**
That contract runs against `FakeEmbedder` and, marked and opt-in, against the
real `FastEmbedEmbedder`; every case in it is a statement about a *correct*
answer -- order, width, normalisation, determinism, not calling a model for
nothing. Everything below is about an answer that is **wrong on the wire**, and
a scripted fake can never produce one: the endpoint is a process this
deployment does not control, reachable over a socket, and each of the six
malformed shapes here is something a remote server is free to send at any
moment without anything in this repository changing.

The one that matters most is the reordering case, and it is the reason this
file exists rather than four extra assertions elsewhere. `Embedder.embed`'s
docstring calls a reordering *"the most damaging bug available in this
milestone"*: title *n*'s vector lands on title *m*, every subsequent
`title_neighbors` row is built from it, and no per-vector assertion -- norm,
width, determinism, count -- can see it. The OpenAI response schema hands back
objects carrying an `index` precisely because arrival order is not promised, so
"the transport happened to preserve it" is the only thing standing between this
project and that bug unless the sort is asserted. It is asserted here **on the
bytes the mock really served**, not on the literal the fixture was built from:
a shuffle case whose premise is a comment passes against an implementation that
never sorts.

Two things this file deliberately does not do. It does not subclass
`EmbedderContract` -- that would be right, and the class it must be added to is
`tests/unit/test_embedder_contract.py`, which this change does not own. And it
never asserts *relevance*: the vectors here are arithmetic, so anything
resembling a similarity claim would pass for a reason unrelated to the code,
which is the vacuous pass this repository has already shipped once.
"""

import json
import math
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from usher.adapters.embedding.openai_compat import (
    RUNTIME,
    OpenAICompatEmbedder,
    checkpoint_of,
)
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

_Handler = Callable[[httpx.Request], httpx.Response]

#: Deliberately not the 1024 the shipped column stores. The adapter is told its
#: width by its caller and asserts what it was told, so a fixture pinning the
#: real number would make the case pass for the deployment's reason rather than
#: for the adapter's -- and would go quiet the day `EMBEDDING_DIMENSIONS` moves
#: again. Eight is enough to make a norm and a width two different facts.
_DIMENSION = 8

#: A plausible bearer token, and every failure case below asserts it is absent
#: from the message. A short marker string would be found by accident inside
#: JSON or a class name.
_KEY = "sk-0000000000000000000000000000000000000000000000"

#: **`.invalid` is reserved by RFC 6761 and can never resolve**, so a case that
#: reached a socket fails rather than talking to somebody. The host is also the
#: subject of the leak sweep: a household may point `USHER_EMBEDDING_BASE_URL`
#: at a provider whose URL carries a token in a path segment, so no message may
#: carry any part of it.
_BASE_URL = "https://embeddings.invalid/v1"

_CHECKPOINT = "BAAI/bge-m3"
_MODEL_NAME = f"{RUNTIME}:{_CHECKPOINT}"

_TEXTS = ["a caretaker inventories a house", "harbour lights", "vane"]


def _unit(seed: float = 1.0) -> list[float]:
    """A genuine unit vector of the declared width, distinct per seed.

    Distinctness is what makes the ordering cases readable: three identical
    unit vectors are re-sorted correctly by an implementation that does not
    sort at all.
    """
    raw = [seed + index for index in range(_DIMENSION)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def _body(
    vectors: Sequence[Sequence[float]], *, indices: Sequence[int] | None = None
) -> dict[str, Any]:
    """The documented `POST /embeddings` response shape.

    `indices` defaults to `0..n-1` in arrival order; passing something else is
    how the reordering and the index-set cases put a real endpoint's freedom on
    the wire.
    """
    order = list(indices) if indices is not None else list(range(len(vectors)))
    return {
        "object": "list",
        "model": _CHECKPOINT,
        "data": [
            {"object": "embedding", "index": index, "embedding": list(vector)}
            for index, vector in zip(order, vectors, strict=True)
        ],
        "usage": {"prompt_tokens": 11, "total_tokens": 11},
    }


def _responds(
    *, status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> _Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json=_body([_unit()]) if body is None else body, headers=headers
        )

    return handler


def _embedder(handler: _Handler, **kwargs: Any) -> OpenAICompatEmbedder:
    kwargs.setdefault("base_url", _BASE_URL)
    kwargs.setdefault("api_key", _KEY)
    kwargs.setdefault("dimension", _DIMENSION)
    return OpenAICompatEmbedder(
        kwargs.pop("model_name", _MODEL_NAME),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


async def _embed(embedder: OpenAICompatEmbedder, texts: Sequence[str]) -> list[list[float]]:
    try:
        return await embedder.embed(list(texts))
    finally:
        await embedder.aclose()


# --------------------------------------------------------------------------
# The request


async def test_the_request_carries_the_checkpoint_and_the_batch_in_order() -> None:
    """Kills sending the prefixed `model_name` to the provider.

    `openai:BAAI/bge-m3` is one string to an operator, to `Settings` and to
    `title_embeddings.model_name`, and two facts to this adapter. The endpoint
    serves a checkpoint and has never heard of the runtime half, so a client
    that sent the whole string would 4xx on every request -- and be parked as
    malformed data, correctly, for a reason no message would name.
    """
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_body([_unit(1.0), _unit(2.0), _unit(3.0)]))

    await _embed(_embedder(handler), _TEXTS)

    assert seen[0]["model"] == _CHECKPOINT
    assert seen[0]["input"] == _TEXTS


async def test_the_credential_is_a_header_and_never_reaches_the_url() -> None:
    """Kills `?api_key=`.

    `HTTPXClientInstrumentor` is wired in `configure_tracing` and records the
    full URL as a span attribute, so a query-parameter credential is written
    into telemetry on every request -- the same reason `OpenAICompatibleClient`
    prefers a bearer token one package over. TMDb v3 forces the query spelling;
    this protocol does not.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_body([_unit()]))

    await _embed(_embedder(handler), ["one"])

    assert seen[0].headers["authorization"] == f"Bearer {_KEY}"
    assert _KEY not in str(seen[0].url)


async def test_no_credential_configured_sends_no_authorization_header() -> None:
    """**The shipped default, not an edge case.** `Settings.embedding_api_key`
    is an empty `SecretStr` and `composition._load_embedder` normalises that to
    `None`, so a local vLLM -- the deployment this runtime exists for -- takes
    this branch on every request. Sending `Bearer None`, or a blank bearer, is
    how a client fails against the one server it was written for.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_body([_unit()]))

    await _embed(_embedder(handler, api_key=None), ["one"])

    assert "authorization" not in seen[0].headers


async def test_a_batch_larger_than_the_batch_size_is_split_and_rejoined_in_order() -> None:
    """Pins `batch_size` as a request boundary rather than a decoration.

    Two properties in one case, and neither implies the other: that the split
    happens at all (a remote endpoint bounds its own input array, and the
    failure is a 4xx on the largest catalogue and never in a test), and that
    the pieces come back concatenated in the order they went out. An
    implementation that split correctly and appended each response as it
    *arrived* would pass every per-chunk assertion and land the second chunk's
    vectors on the first chunk's titles.
    """
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = list(json.loads(request.content)["input"])
        seen.append(inputs)
        return httpx.Response(
            200, json=_body([_unit(float(_TEXTS.index(text) + 1)) for text in inputs])
        )

    vectors = await _embed(_embedder(handler, batch_size=2), _TEXTS)

    assert seen == [_TEXTS[:2], _TEXTS[2:]], "the premise: this really was two requests"
    assert vectors == [_unit(1.0), _unit(2.0), _unit(3.0)]


async def test_an_empty_batch_is_not_a_call() -> None:
    """The port states this as a contract rather than an optimisation, and here
    the cost is a round trip and a billable request rather than a GPU stall.

    Asserted on the request that did not happen, never on the empty result: an
    implementation that sent `{"input": []}` and got an empty `data` array back
    returns `[]` too, and would be indistinguishable.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_body([]))

    embedder = _embedder(handler)
    try:
        assert await embedder.embed([]) == []
    finally:
        await embedder.aclose()

    assert seen == [], "an empty batch reached the network"


# --------------------------------------------------------------------------
# Order, which is the port's contract


async def test_the_vectors_come_back_in_the_order_the_texts_went_in() -> None:
    """The control. Without it every case below passes against an
    implementation that refuses everything.
    """
    handler = _responds(body=_body([_unit(1.0), _unit(2.0), _unit(3.0)]))

    vectors = await _embed(_embedder(handler), _TEXTS)

    assert vectors == [_unit(1.0), _unit(2.0), _unit(3.0)]


async def test_a_shuffled_response_is_sorted_back_by_index() -> None:
    """**The case this file exists for.**

    `Embedder.embed` calls a reordering the most damaging bug available here:
    title *n*'s vector is stored against title *m*, `title_neighbors` is built
    from it, and nothing -- not the norm, not the width, not the count, not
    determinism -- can see it afterwards. The OpenAI response schema puts an
    `index` on every embedding object precisely because arrival order is not
    part of the contract, so an implementation that trusts arrival order is
    correct only for as long as one particular server keeps being tidy.

    **Both premises are read off the bytes the mock served**, not off the
    literal the fixture was built from. The first says the response really was
    out of order; the second says arrival order and the answer are different
    lists, so an implementation that never sorts cannot satisfy this case by
    accident. Without them a green result here is compatible with a transport
    that quietly re-ordered on the way in.
    """
    served: list[dict[str, Any]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        body = _body([_unit(3.0), _unit(1.0), _unit(2.0)], indices=[2, 0, 1])
        served.append(body)
        return httpx.Response(200, json=body)

    vectors = await _embed(_embedder(handler), _TEXTS)

    arrived = [entry["index"] for entry in served[0]["data"]]
    assert arrived != sorted(arrived), "the premise: the mock served them in index order"
    assert [entry["embedding"] for entry in served[0]["data"]] != vectors, (
        "the premise: arrival order and the sorted answer are the same list"
    )
    assert vectors == [_unit(1.0), _unit(2.0), _unit(3.0)]


async def test_fewer_vectors_than_texts_is_malformed() -> None:
    """A provider that deduplicated identical inputs, or truncated an
    over-long batch, answers 200 with a short array. Every vector in it is
    perfectly good and every one after the gap is attributed to the wrong
    title.

    `PortDataMalformed` rather than retryable: no backoff makes a server return
    a different number of vectors for the same input, so `JobWorker` parks the
    job with what was wrong instead of spending five attempts to be told again.
    """
    handler = _responds(body=_body([_unit(1.0), _unit(2.0)]))

    with pytest.raises(PortDataMalformed, match="2 vectors for 3 texts"):
        await _embed(_embedder(handler), _TEXTS)


async def test_a_duplicated_index_is_malformed() -> None:
    """The count is right and the answer is still wrong, which is why the
    count check is not enough. Two objects both claiming `index: 0` leave one
    input with no vector at all -- an implementation keyed on a dict would
    silently answer with the second one twice, and an implementation trusting
    arrival order would not notice anything.
    """
    handler = _responds(body=_body([_unit(1.0), _unit(2.0)], indices=[0, 0]))

    with pytest.raises(PortDataMalformed, match="index 0 twice"):
        await _embed(_embedder(handler), _TEXTS[:2])


async def test_an_index_set_that_is_not_zero_to_n_minus_one_is_malformed() -> None:
    """Right count, no duplicate, and still not a permutation of the input.

    A one-based server -- or one paging its own answer -- returns `1, 2` for
    two texts. Sorting that by index yields a list of exactly the right length
    in exactly the wrong alignment, which is the reordering bug arriving
    through the check that was supposed to catch it.
    """
    handler = _responds(body=_body([_unit(1.0), _unit(2.0)], indices=[1, 2]))

    with pytest.raises(PortDataMalformed, match="indices"):
        await _embed(_embedder(handler), _TEXTS[:2])


# --------------------------------------------------------------------------
# The first-batch checks


async def test_a_vector_that_is_not_unit_normalised_is_refused() -> None:
    """**The mutation with the largest silent blast radius, and the reason it
    is asserted here rather than taken from a model card.**

    Normalisation is a property of the *checkpoint* -- a third module after
    Transformer and Pooling -- and the same backbone with it removed returns
    norms 8.99-9.46, which makes every dot-product score ~85x too large: a
    plausible-looking ranking that is wrong everywhere, with nothing raising.
    `EmbedderContract` cannot see it, because it runs against the model this
    deployment shipped with rather than the one the endpoint is serving now,
    and *now* is the operative word for a remote model nobody here restarts.

    Verified live on 2026-08-13: bge-m3 through the reference vLLM returns norm
    exactly 1.0, so the tolerance has four orders of magnitude of headroom
    against the failure and cannot be tripped by float noise. Fails:
    `_NORM_TOLERANCE = 10.0`, or deleting the check.
    """
    handler = _responds(body=_body([[value * 9.0 for value in _unit()]]))

    with pytest.raises(PortDataMalformed, match="norm"):
        await _embed(_embedder(handler), ["one"])


async def test_a_vector_of_the_wrong_width_is_refused() -> None:
    """**The check `FastEmbedEmbedder` does not have, and the whole reason this
    adapter takes a `dimension`.**

    An in-process model's width is a property of the file that was loaded; this
    one's is a property of a server somebody else can restart against different
    weights, with nothing in this repository changing and no error anywhere.
    The stored column is `halfvec(EMBEDDING_DIMENSIONS)`, so the alternative to
    this check is asyncpg refusing the write one `index` job at a time with the
    expected width named by neither side.

    The vector here is a genuine unit vector, so **only** the width check can
    fire -- otherwise this case and the norm case above are one case with two
    names.
    """
    wide = [value / math.sqrt(_DIMENSION + 1) for value in [1.0] * (_DIMENSION + 1)]
    handler = _responds(body=_body([wide]))

    with pytest.raises(PortDataMalformed, match="width"):
        await _embed(_embedder(handler), ["one"])


async def test_the_first_batch_checks_run_once_and_not_per_batch() -> None:
    """The other half of the two cases above, and the half that keeps a square
    root per vector off the hot path.

    Both checks answer a question about *which model the endpoint is serving*.
    That can change -- which is the whole argument for checking at all -- but
    it cannot change usefully often: an operator who swaps the served model
    restarts nothing here, and re-asking per batch would cost a per-vector
    square root on every `index` job forever to catch it a few minutes sooner.
    Same call, and the same `self._checked` flag, as `FastEmbedEmbedder`.
    """
    answers = [_body([_unit()]), _body([[value * 9.0 for value in _unit()]])]
    sent = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal sent
        answer = answers[min(sent, len(answers) - 1)]
        sent += 1
        return httpx.Response(200, json=answer)

    embedder = _embedder(handler)
    try:
        first = await embedder.embed(["one"])
        second = await embedder.embed(["two"])
    finally:
        await embedder.aclose()

    assert len(first[0]) == _DIMENSION
    assert len(second) == 1, "the norm was re-checked on a later batch"


# --------------------------------------------------------------------------
# The status taxonomy


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_credential_is_auth_failed(status: int) -> None:
    """A key is a key: no cooldown and no negative cache, the way
    `usher.adapters.http.port_error_for` decided it for the two upstreams that
    got there first. The queue's own backoff spaces the retries out.
    """
    with pytest.raises(PortAuthFailed):
        await _embed(_embedder(_responds(status=status, body={})), ["one"])


async def test_a_429_is_rate_limited_and_reads_retry_after() -> None:
    """The hint is optional and both RFC 9110 forms are parsed by the shared
    helper, which exists because the same `Retry-After` bug was written twice.
    """
    handler = _responds(status=429, body={}, headers={"retry-after": "7"})

    with pytest.raises(PortRateLimited) as raised:
        await _embed(_embedder(handler), ["one"])

    assert raised.value.retry_after == pytest.approx(7.0)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_5xx_is_unavailable(status: int) -> None:
    """A model still loading, a GPU held by something else, a proxy with no
    upstream. All three are a bad five minutes that a restart or a wait fixes,
    so `JobWorker` backs off rather than parking work whose only problem was
    the clock -- a park needs a human to release it.
    """
    with pytest.raises(PortUnavailable):
        await _embed(_embedder(_responds(status=status, body={})), ["one"])


async def test_a_transport_failure_is_unavailable() -> None:
    """The commonest failure this adapter has and the one `FastEmbedEmbedder`
    cannot have: nothing is listening on the configured port.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with pytest.raises(PortUnavailable):
        await _embed(_embedder(handler), ["one"])


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_a_permanent_4xx_is_malformed_not_unavailable(status: int) -> None:
    """M4 measured this against TMDb and M8 confirmed it against a live
    completion endpoint: a 4xx that is not a 429 cannot become an answer by
    being sent again, so five rate-limited retries reach the identical answer
    and then park with "upstream unavailable" rather than with what was wrong.

    Here the realistic three are a model name the server does not serve (404),
    a batch or a text over the server's own input bound (400), and a schema it
    will not accept (422). The fix for each is a setting, not a wait.
    """
    with pytest.raises(PortDataMalformed):
        await _embed(_embedder(_responds(status=status, body={})), ["one"])


async def test_a_200_that_is_not_the_documented_shape_is_malformed() -> None:
    """A reverse proxy answering an HTML login page under a 200 is the
    realistic way to get here, and a raw `json.JSONDecodeError` is not
    something a caller written against `usher.ports.errors` can catch.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>sign in</html>", headers={"content-type": "application/json"}
        )

    with pytest.raises(PortDataMalformed):
        await _embed(_embedder(handler), ["one"])


@pytest.mark.parametrize(
    "body",
    [
        {"object": "list"},
        {"data": "not an array"},
        {"data": [{"index": 0}]},
        {"data": [{"index": 0, "embedding": "gASVdQ=="}]},
        {"data": [{"index": "0", "embedding": [1.0]}]},
        {"data": [{"index": True, "embedding": [1.0]}]},
        {"data": ["not an object"]},
    ],
    ids=[
        "no-data",
        "data-not-a-list",
        "no-embedding",
        "base64",
        "index-a-string",
        "index-a-bool",
        "entry-not-an-object",
    ],
)
async def test_an_embedding_object_that_is_not_the_documented_shape_is_malformed(
    body: dict[str, Any],
) -> None:
    """Seven shapes, and two of them are the ones a reader would not write.

    `"embedding": "gASVdQ=="` is `encoding_format: "base64"`, which the OpenAI
    schema permits and the official client asks for by default -- so a provider
    that made it *its* default answers a `str` where this port promises
    `list[float]`, and `float("gASVdQ==")` raises `ValueError` rather than
    anything a caller can catch. `"index": true` is the other: `bool` is a
    subclass of `int` in Python, so a truthy JSON literal is read as index 1 by
    any check spelled `isinstance(index, int)`, which silently moves a vector
    onto the wrong title -- the same defect the sort exists to prevent,
    arriving through the sort's own key.
    """
    with pytest.raises(PortDataMalformed):
        await _embed(_embedder(_responds(body=body)), ["one"])


async def test_no_failure_message_carries_the_credential_or_the_base_url() -> None:
    """PRD 08: credentials are never logged, error paths included -- and an
    httpx transport exception's own text frequently contains the request URL,
    which is why nothing here interpolates `exc` rather than
    `type(exc).__name__`.

    The base URL is in scope alongside the key and not as a courtesy: a
    household may point `USHER_EMBEDDING_BASE_URL` at a provider whose URL
    carries a token in a path segment, which is the same argument
    `OpenAICompatibleClient` makes for passing no `detail` to the shared
    helpers. Swept over every arm that can raise, because a leak needs only
    one.
    """
    for status in (400, 401, 429, 500):
        with pytest.raises(Exception) as raised:  # any port error will do
            await _embed(_embedder(_responds(status=status, body={})), ["one"])
        assert _KEY not in str(raised.value)
        assert "embeddings.invalid" not in str(raised.value)

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {_BASE_URL}?api_key={_KEY}")

    with pytest.raises(PortUnavailable) as transport_failure:
        await _embed(_embedder(refuse), ["one"])
    assert _KEY not in str(transport_failure.value)
    assert "embeddings.invalid" not in str(transport_failure.value)

    malformed = _responds(body=_body([_unit(1.0), _unit(2.0)]))
    with pytest.raises(PortDataMalformed) as wrong_shape:
        await _embed(_embedder(malformed), _TEXTS)
    assert _KEY not in str(wrong_shape.value)
    assert "embeddings.invalid" not in str(wrong_shape.value)


# --------------------------------------------------------------------------
# The fingerprint


def test_the_model_name_is_the_whole_prefixed_string() -> None:
    """What goes on the wire and what goes in the column are two strings, and
    this is the column's.

    `title_embeddings.model_name` is the fingerprint the stale predicate
    compares, so recording the checkpoint alone would make a runtime swap --
    the same weights, a different server, a different tokenizer build --
    invisible: every stored vector would keep matching a model that no longer
    produced it, with no migration to notice and no error to read.
    """
    embedder = _embedder(_responds())

    assert embedder.model_name == _MODEL_NAME
    assert embedder.dimension == _DIMENSION


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("openai:BAAI/bge-m3", "BAAI/bge-m3"),
        # No prefix: taken as the checkpoint, so an operator who wrote a bare
        # name gets the model rather than a parse error. `model_name` still
        # records what was configured, which is what makes a swap detectable.
        ("BAAI/bge-m3", "BAAI/bge-m3"),
        # A colon *inside* the checkpoint -- a revision or a served-model alias
        # -- which is the whole reason this is `partition` and not
        # `rpartition`. Without this row the two spell the same thing for every
        # id carrying one colon, and the mutation survives.
        ("openai:BAAI/bge-m3:latest", "BAAI/bge-m3:latest"),
        # Another runtime's prefix is not this one's to strip. Left whole, the
        # request 4xxs on a model name the server does not serve; stripped, a
        # fastembed-configured deployment would silently be served by an
        # OpenAI-compatible endpoint under a fastembed fingerprint -- the exact
        # confusion `model_name` carries a runtime to prevent.
        ("fastembed:BAAI/bge-small-en-v1.5", "fastembed:BAAI/bge-small-en-v1.5"),
    ],
)
def test_the_runtime_prefix_splits_on_the_first_colon(configured: str, expected: str) -> None:
    """Identical to `FastEmbedEmbedder`'s split, deliberately: the two adapters
    are chosen between by `composition._load_embedder` on the same `partition`,
    so a divergence here would make one string mean two models.
    """
    assert checkpoint_of(configured) == expected


# --------------------------------------------------------------------------
# Lifecycle


async def test_aclose_closes_the_client_and_a_later_call_is_unavailable() -> None:
    """`composition.embedder` returns `built.aclose` as the release callable
    and every entry point calls it in a `finally`, so this is the only thing
    standing between a `usher index --backfill` and a leaked connection pool.

    The second assertion is the one with teeth: a closed `httpx.AsyncClient`
    raises a bare `builtins.RuntimeError`, which is not a `UsherPortError` and
    would escape every `except UsherPortError` in `services/` to take the
    worker process down instead of parking one job. `UNTRANSLATED_FAILURES`
    carries `RuntimeError` for exactly this.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(_responds()))
    embedder = OpenAICompatEmbedder(
        _MODEL_NAME, base_url=_BASE_URL, dimension=_DIMENSION, client=client
    )

    await embedder.embed(["one"])
    await embedder.aclose()
    await embedder.aclose()

    assert client.is_closed
    with pytest.raises(PortUnavailable):
        await embedder.embed(["one"])
