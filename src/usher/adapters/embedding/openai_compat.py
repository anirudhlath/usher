"""`Embedder` over `POST {base_url}/embeddings`, and the three checks that
exist because the model is somebody else's process.

**Why a second `Embedder` at all, which is a fact about a library rather than
a preference.** `fastembed` 0.8.0 does not ship `BAAI/bge-m3` -- enumerated on
2026-08-13 across all five of its model classes (`TextEmbedding`,
`SparseTextEmbedding`, `LateInteractionTextEmbedding`, `ImageEmbedding` and
`LateInteractionMultimodalEmbedding`), and it is in none of them. So the
choice was not "which runtime is nicer" but "which of `bge-m3` and an
in-process model does this deployment want", and it wants `bge-m3` served by
the local vLLM it already runs. ADR-0022's argument for `fastembed` over
`sentence-transformers` is untouched and `FastEmbedEmbedder` still ships: this
adapter is a *second* runtime, selected by the `openai:` prefix on
`Settings.embedding_model`, not a replacement.

**Everything below follows from the model being remote, and each item is
something `FastEmbedEmbedder` cannot get wrong.**

- **Order is re-established, never assumed.** `fastembed` hands back an array
  positionally; this protocol hands back objects carrying an `index`, and does
  so precisely because arrival order is not part of the contract. Anything
  between here and the server -- a load balancer, a batching scheduler, a
  provider that fans a batch across workers -- is free to reorder, and
  `Embedder.embed`'s docstring names the consequence: title *n*'s vector
  lands on title *m*, `title_neighbors` is built from it, and no per-vector
  assertion can see it afterwards. So the vectors are sorted by `index`, and
  the index set is checked to be exactly `range(len(texts))` -- a count check
  alone is satisfied by a duplicate, and a duplicate is a missing vector
  wearing another one's number.
- **The width is asserted.** `FastEmbedEmbedder` knows its width from the file
  it loaded and hard-codes it; here the width is a property of whatever the
  operator most recently told vLLM to serve, and that can change with nothing
  in this repository changing and no error anywhere. The stored column is
  `halfvec(EMBEDDING_DIMENSIONS)`, so without this check the failure is
  asyncpg refusing one `index` job at a time with the expected width named by
  neither side. This is the whole reason the constructor takes a `dimension`.
- **The norm is asserted, not read off a model card**, and this half is
  inherited rather than new. Normalisation is a property of the *checkpoint*
  -- a third module after Transformer and Pooling -- and the same backbone
  with it removed returns norms **8.99-9.46**, which makes every dot-product
  score ~85x too large: a plausible ranking that is wrong everywhere, raising
  nothing. Verified live against the reference endpoint on **2026-08-13**:
  `bge-m3` through vLLM returns norm **exactly 1.0**, so `_NORM_TOLERANCE`
  has four orders of magnitude of headroom against the failure it is for.
  `EmbedderContract` cannot cover this, and covers it even less well here than
  it does for `fastembed`: it runs against the model this deployment shipped
  with, and the served model is the one thing about this adapter that can
  change while the process lives.

**Two shapes on the wire that are decided rather than defaulted.**
`encoding_format` is deliberately **not sent**. The schema permits `float` and
`base64` and the official client asks for `base64`, so a provider could
reasonably make that its default and answer a `str` where this port promises
`list[float]` -- but the reference endpoint answered floats without the field
on 2026-08-13 (which is how the norm above was read), and a field this
deployment has never put on the wire is a 4xx nobody has ruled out. The base64
case is therefore a legible refusal rather than an untested request parameter,
which is a judgement about which risk is measured and not a claim that one is
impossible. And **`dimensions` is not sent either**: on a provider that honours
it, it would silently truncate to whatever this deployment asked for and make
the width check agree with itself; on one that does not, it is a 4xx on every
request. The width is a fact to check, not a thing to request.

**The credential is a header, never a URL, and never a message.**
`HTTPXClientInstrumentor` records the full URL as a span attribute, so a
query-parameter key is written into telemetry on every request. Nothing here
interpolates `base_url` or the key into an exception -- not even through the
shared helpers' `detail`, which is why `usher.adapters.http.decode_json` takes
it as optional -- because a household may be pointed at a provider whose URL
carries a token in a path segment. The key arrives unwrapped (the composition
root unwraps at the point of use, per CLAUDE.md) and is re-wrapped in a
`SecretStr` here rather than held as a bare `str`, so no `repr` of this object
can carry it. `telemetry.configure_logging` sets `diagnose=False`, so loguru
would not render a local today -- the wrap defends the paths that setting does
not cover (a pytest traceback, any future sink) and costs one call.

**Upstream: the endpoint named by `USHER_EMBEDDING_MODEL`'s `openai:` runtime
prefix. Deliberately unthrottled** (M10's S3; the enumeration is
`tests/unit/test_outbound_call_sites.py`), on `llm/openai_compatible.py`'s
reasoning exactly: no published ceiling, no measured one, and `KIND_CONCURRENCY`
caps `index` at **1 in flight**, so the concurrency table is the bound and a
requests-per-second gate would be a second ceiling above a lower one.
**Stated here rather than inherited, because this adapter is the one of the two
that runs for hours**: a `usher index --backfill` over a 1.27M-title catalog is
a long serialised stream of `POST /embeddings`, not one call a night, so
"the concurrency cap makes a rate limit unreachable" is a claim worth writing
down where somebody raising `USHER_JOB_CONCURRENCY` will read it. Raise the
`index` cap and this decision is reopened.
"""

import math
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from usher.adapters.http import UNTRANSLATED_FAILURES, decode_json, port_error_for
from usher.ports.embedding import Embedder
from usher.ports.errors import PortDataMalformed, PortUnavailable

# The runtime prefix `model_name` carries, and the separator that splits it
# back off for the request body. `openai:BAAI/bge-m3` is one string to an
# operator and to the `model_name` column, and two facts here.
RUNTIME = "openai"
_SEPARATOR = ":"

_EMBEDDINGS_PATH = "/embeddings"

# How this endpoint is named in a message. A constant and never `base_url`,
# never a URL and never a path built from one -- `OpenAICompatibleClient`'s
# `_ENDPOINT` for the same reason, and the reason nothing below passes a
# `detail` to `usher.adapters.http`.
_ENDPOINT = "the embedding endpoint"

# The same tolerance `FastEmbedEmbedder` uses, deliberately: the measured norm
# here is exactly 1.0 so any tolerance would admit it, and a *different* number
# would make a reader comparing the two adapters work out whether the
# difference meant something. It is four orders of magnitude below the 8.99 a
# missing Normalize module produces, so it cannot be passed by the failure it
# exists to catch.
_NORM_TOLERANCE = 1e-4


def checkpoint_of(model_name: str) -> str:
    """`openai:BAAI/bge-m3` -> `BAAI/bge-m3`.

    `partition`, not `rpartition`: a checkpoint id contains `/` and may
    contain `:` in a revision or served-model alias, and it is the *first*
    colon that separates the runtime. A bare name with no prefix is taken as
    the checkpoint, so an operator who wrote one gets the model rather than a
    parse error -- the `model_name` column still records what this deployment
    was configured with, which is what makes a swap detectable.

    Another runtime's prefix is left whole rather than stripped. The request
    then 4xxs on a model name the server does not serve, which is loud;
    stripping it would serve a `fastembed:`-configured deployment from an
    OpenAI-compatible endpoint under a fastembed fingerprint, which is the
    exact confusion `model_name` carries a runtime to prevent.

    Byte-identical to `FastEmbedEmbedder`'s split, deliberately:
    `composition._load_embedder` chooses between the two adapters on the same
    `partition`, so a divergence here would make one string mean two models.
    """
    runtime, separator, checkpoint = model_name.partition(_SEPARATOR)
    return checkpoint if separator and runtime == RUNTIME else model_name


class OpenAICompatEmbedder(Embedder):
    """One HTTP client, held for the life of the process.

    Constructed by `usher.composition._load_embedder` and by nothing else. The
    client is a process-lifetime resource for the same reason the ONNX session
    is: `build_worker` runs once per worker *pass* at a 5 s floor, and a client
    per pass is a fresh connection pool -- a TCP handshake and, against a
    hosted provider, a TLS one -- for every batch, forever, with nothing in the
    logs saying so.

    **Ownership is stated rather than left ambiguous, and it differs from
    `OpenAICompatibleClient`'s deliberately.** That class injects a
    `transport` and always owns its client, because ownership of a thing
    `aclose` promises to release should never be a question. Here the injected
    object is the client itself, and an injected client is **adopted**:
    `aclose` closes whatever this object holds. The port is what forces the
    choice -- `Embedder.aclose` is the release callable `composition.embedder`
    hands to every entry point's `finally`, so this object must have something
    to close, and a caller keeping a client alive past the embedder that was
    given it has no way to say so. Nothing in `src/` passes one.
    """

    def __init__(
        self,
        model_name: str,
        *,
        base_url: str,
        api_key: str | None = None,
        dimension: int,
        batch_size: int = 16,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model_name = model_name
        # What goes on the wire, computed once. The endpoint serves a
        # checkpoint and has never heard of the runtime half of the string.
        self._checkpoint = checkpoint_of(model_name)
        self._base_url = base_url.rstrip("/")
        # Re-wrapped rather than held bare: see the module docstring. `None`
        # and `""` are the same thing here -- the local-server case, no header
        # at all -- and the composition root already normalises the empty
        # `SecretStr` it reads from `Settings`, so this is a second door on the
        # same decision rather than a competing one.
        self._api_key = SecretStr(api_key) if api_key else None
        self._dimension = dimension
        self._batch_size = batch_size
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        # One check, on the first batch, for the reason `embed` gives. A
        # per-batch check would cost a square root per vector on a hot path to
        # re-answer a question about the served model that an operator changes
        # by restarting a server this process cannot see.
        self._checked = False

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            # Before the client is touched: an empty batch is an empty result
            # and **not a call**. On a metered endpoint that is the difference
            # between a no-op and a billed request; on any endpoint it is a
            # round trip for nothing.
            return []
        batch = list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(batch), self._batch_size):
            # One request per `batch_size` texts, because the bound is the
            # *server's* input array and not this process's memory. Each chunk
            # is checked against its own length -- the protocol's `index` is
            # relative to the request that carried it -- and the chunks are
            # concatenated in the order they were sent, which is the half of
            # the port's ordering contract that survives the split.
            chunk = batch[start : start + self._batch_size]
            vectors.extend(self._vectors(await self._post(chunk), len(chunk)))
        if not self._checked:
            self._checked = True
            self._check_first(vectors[0])
        return vectors

    async def aclose(self) -> None:
        """Release the connection pool. Idempotent -- `httpx.AsyncClient.aclose`
        is, and `composition.embedder`'s release callable may be reached twice
        by an entry point that closes in a `finally` under a failure."""
        await self._client.aclose()

    # ----------------------------------------------------------------- send

    async def _post(self, chunk: list[str]) -> httpx.Response:
        """The request, and the two error families a status can carry.

        The status ladder is `usher.adapters.http.port_error_for` unchanged --
        429, then 401/403, then any other 4xx except 408, then everything at or
        above 400 -- and it is shared rather than restated for the reason that
        module exists: the third copy of `decode_json` was the only one that
        had learned about `RecursionError`, so two adapters were one deeply
        nested payload away from taking the worker process down. The ladder's
        placement of a non-429 4xx in `PortDataMalformed` is right here too and
        for the same measured reason: a model name the server does not serve, a
        batch over its input bound and a schema it will not accept are all
        permanent for that request, so five rate-limited retries reach the
        identical answer and then park with "upstream unavailable" instead of
        with what was wrong.
        """
        headers = {"content-type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        payload = {"model": self._checkpoint, "input": chunk}
        try:
            response = await self._client.post(
                f"{self._base_url}{_EMBEDDINGS_PATH}", json=payload, headers=headers
            )
        except UNTRANSLATED_FAILURES as exc:
            # `type(exc).__name__`, never `exc`: httpx's own text for several
            # transport failures includes the request URL. `RuntimeError` is in
            # that tuple for the closed-client case, which would otherwise
            # escape every `except UsherPortError` in `services/` and take the
            # worker down instead of parking one job.
            raise PortUnavailable(f"POST {_EMBEDDINGS_PATH} failed: {type(exc).__name__}") from exc
        error = port_error_for(response, what=_ENDPOINT, request_line=f"POST {_EMBEDDINGS_PATH}")
        if error is not None:
            raise error
        return response

    # ---------------------------------------------------------------- parse

    def _vectors(self, response: httpx.Response, expected: int) -> list[list[float]]:
        """The response's vectors, in input order, or `PortDataMalformed`.

        **Four ways the alignment can be wrong and only the first is the
        obvious one**, which is why all four are checked rather than the count
        alone: too few or too many objects, one `index` appearing twice, and an
        index set that is the right size and not `range(n)` -- a one-based
        server answers `1, 2` for two texts, and sorting *that* by index yields
        a list of exactly the right length in exactly the wrong alignment.
        Every one of them is the reordering `Embedder.embed` calls the most
        damaging bug available here, and none of them is visible to any
        assertion about a vector.

        `PortDataMalformed` throughout rather than retryable: no backoff makes
        a server answer a different shape for the same input, so `JobWorker`
        parks the job with what was wrong.

        **The index-set check earns its keep twice over, measured by planting
        its removal**: without it the final comprehension raises a bare
        `KeyError` for the absent position -- not a `UsherPortError`, so it
        escapes every `except UsherPortError` in `services/` and takes the
        worker process down instead of parking one job. Same family as
        `OpenAICompatibleClient._content`'s note about `json.loads(None)`'s
        `TypeError`, one adapter over.
        """
        body = decode_json(response, what=_ENDPOINT)
        data = body.get("data")
        if not isinstance(data, list):
            raise PortDataMalformed(f"{_ENDPOINT} returned no data array")
        if len(data) != expected:
            raise PortDataMalformed(
                f"{self._model_name} returned {len(data)} vectors for {expected} texts"
            )
        by_index: dict[int, list[float]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                raise PortDataMalformed(
                    f"{_ENDPOINT} returned a {type(entry).__name__} where an embedding was expected"
                )
            index = entry.get("index")
            # `isinstance(True, int)` is `True`, so the `bool` exclusion is not
            # decoration: a JSON `true` read as index 1 moves a vector onto the
            # wrong title through the very key the sort trusts.
            if not isinstance(index, int) or isinstance(index, bool):
                raise PortDataMalformed(f"{_ENDPOINT} returned an embedding with no integer index")
            if index in by_index:
                raise PortDataMalformed(f"{_ENDPOINT} returned index {index} twice")
            by_index[index] = self._vector(entry.get("embedding"))
        if by_index.keys() != set(range(expected)):
            # Sorted by the numbers on the wire, so the numbers have to be the
            # positions in the batch. Reported as a count and a range, never as
            # the set itself: an endpoint that answered thousands of indices
            # would put its whole answer in a log line.
            raise PortDataMalformed(
                f"{_ENDPOINT} returned {len(by_index)} indices that are not 0..{expected - 1}"
            )
        return [by_index[index] for index in range(expected)]

    def _vector(self, embedding: Any) -> list[float]:
        """One embedding, as the `list[float]` the port promises.

        A `str` here is `encoding_format: "base64"`, which the schema permits
        and the official client asks for by default -- so it is a shape a
        provider may reasonably answer with, and `float("gASV")` raises
        `ValueError`, which is not a `UsherPortError`. Named in the message
        because the fix is a provider setting rather than anything here.
        """
        if not isinstance(embedding, list):
            raise PortDataMalformed(
                f"{_ENDPOINT} returned a {type(embedding).__name__} embedding, not an array",
                detail="a string here is encoding_format=base64, which this port cannot read",
            )
        for value in embedding:
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise PortDataMalformed(
                    f"{_ENDPOINT} returned a {type(value).__name__} inside an embedding"
                )
        return [float(value) for value in embedding]

    def _check_first(self, vector: list[float]) -> None:
        """The once-per-process checks, in the order their diagnoses depend on.

        **Width first**, because it answers *which model is this* and the norm
        answers *is this model's Normalize module intact* -- and a wrong-width
        vector makes the second question's answer meaningless. A model swapped
        underneath this process is far and away the likeliest cause of both,
        and it is the width that names it.

        Both are checked **before** anything reaches the `halfvec` cast, never
        after: post-cast norm drift is 1.21e-04 against 1.19e-07, a 1000x
        change, so the same norm check over a stored vector fails on a healthy
        model.
        """
        if len(vector) != self._dimension:
            raise PortDataMalformed(
                f"{self._model_name} returned a vector of width "
                f"{len(vector)}, not {self._dimension}",
                detail="the endpoint is serving a different model, or the width is misconfigured",
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            raise PortDataMalformed(
                f"{self._model_name} returned a vector of norm {norm:.4f}, not 1.0",
                detail="this checkpoint's Normalize module is missing or was replaced",
            )


__all__ = ["RUNTIME", "OpenAICompatEmbedder", "checkpoint_of"]
