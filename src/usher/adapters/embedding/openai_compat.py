"""`Embedder` over `POST {base_url}/embeddings`, and the checks it needs.

The model is somebody else's process and can change underneath this one.
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

# The same tolerance `FastEmbedEmbedder` uses, deliberately: a different number
# would make a reader comparing the two adapters work out whether the
# difference meant something.
_NORM_TOLERANCE = 1e-4


def checkpoint_of(model_name: str) -> str:
    """`openai:BAAI/bge-m3` -> `BAAI/bge-m3`.

    `partition`, not `rpartition`: a checkpoint id contains `/` and may contain
    `:` in a revision or served-model alias, so it is the *first* colon that
    separates the runtime. A bare name with no prefix is taken as the
    checkpoint, so an operator who wrote one gets the model, not a parse error.

    Another runtime's prefix is left whole rather than stripped: the request
    then 4xxs loudly, where stripping would serve a `fastembed:`-configured
    deployment from here under a fastembed fingerprint.

    Byte-identical to `FastEmbedEmbedder`'s split, because
    `composition._load_embedder` chooses between the two adapters on the same
    `partition` -- a divergence would make one string mean two models.
    """
    runtime, separator, checkpoint = model_name.partition(_SEPARATOR)
    return checkpoint if separator and runtime == RUNTIME else model_name


class OpenAICompatEmbedder(Embedder):
    """One HTTP client, held for the life of the process.

    Constructed by `usher.composition._load_embedder` and by nothing else. The
    client is a process-lifetime resource for the same reason the ONNX session
    is: `build_worker` runs once per worker *pass* at a 5 s floor, and a client
    per pass is a fresh connection pool -- a TCP handshake, and against a hosted
    provider a TLS one -- for every batch, with nothing in the logs saying so.

    An injected client is **adopted**: `aclose` closes whatever this object
    holds. `Embedder.aclose` is the release callable `composition.embedder`
    hands to every entry point's `finally`, so this object must have something
    to close and a caller cannot ask to keep its client. Nothing in `src/`
    passes one.
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
        # Re-wrapped in `SecretStr` so no repr can leak it. `None` and `""` are
        # the same thing here -- the local-server case, no header at all -- and
        # the composition root already normalises the empty one it reads.
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
            # One request per `batch_size` texts, because the bound is the *server's*
            # input array and not this process's memory.
            chunk = batch[start : start + self._batch_size]
            vectors.extend(self._vectors(await self._post(chunk), len(chunk)))
        if not self._checked:
            self._checked = True
            self._check_first(vectors[0])
        return vectors

    async def aclose(self) -> None:
        """Release the connection pool.

        Idempotent -- `httpx.AsyncClient.aclose` is, and `composition.embedder`'s
        release callable may be reached twice by an entry point that closes in a
        `finally` under a failure.
        """
        await self._client.aclose()

    # ----------------------------------------------------------------- send

    async def _post(self, chunk: list[str]) -> httpx.Response:
        """The request, and the two error families a status can carry.

        The status ladder is `usher.adapters.http.port_error_for` unchanged --
        429, then 401/403, then any other 4xx except 408, then everything at or
        above 400 -- shared rather than restated so one adapter cannot learn
        about a payload shape the others have not. Its placement of a non-429
        4xx in `PortDataMalformed` holds here too: a model the server does not
        serve, a batch over its input bound and a schema it will not accept are
        all permanent for that request, so retrying reaches the same answer and
        then parks with "upstream unavailable" instead of with what was wrong.
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
            # escape every `except UsherPortError` in `services/`.
            raise PortUnavailable(f"POST {_EMBEDDINGS_PATH} failed: {type(exc).__name__}") from exc
        error = port_error_for(response, what=_ENDPOINT, request_line=f"POST {_EMBEDDINGS_PATH}")
        if error is not None:
            raise error
        return response

    # ---------------------------------------------------------------- parse

    def _vectors(self, response: httpx.Response, expected: int) -> list[list[float]]:
        """The response's vectors, in input order, or `PortDataMalformed`."""
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
            # the set: thousands of indices would land in one log line.
            raise PortDataMalformed(
                f"{_ENDPOINT} returned {len(by_index)} indices that are not 0..{expected - 1}"
            )
        return [by_index[index] for index in range(expected)]

    def _vector(self, embedding: Any) -> list[float]:
        """One embedding, as the `list[float]` the port promises.

        A `str` here is `encoding_format: "base64"`, which the schema permits and
        the official client asks for by default, so a provider may reasonably
        answer with it. Named in the message because the fix is a provider
        setting rather than anything here.
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

        Width first: it answers *which model is this*, and a wrong-width vector
        makes the norm's answer meaningless. A model swapped underneath this
        process is the likeliest cause of both, and the width names it.

        Both run *before* anything reaches the `halfvec` cast: the cast's own
        rounding drifts the norm past this tolerance, so the same check over a
        stored vector fails on a healthy model.
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
