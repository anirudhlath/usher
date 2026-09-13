"""PRD 03 stage 4's queued half: one title, one vector, one fingerprint."""

import time
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger
from opentelemetry import metrics, trace

from usher.ports.embedding import Embedder
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import (
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
    TitleRepository,
)
from usher.services.search import compose_document

_tracer = trace.get_tracer("usher.index")
_meter = metrics.get_meter("usher.index")

# PRD 10's name, and **no labels, which is a decision rather than an omission**: the
# obvious label is `model`, and adding one makes the series unqueryable by the
# documented panel while looking like an improvement.
_embedding_duration = _meter.create_histogram(
    "usher.embedding.duration", unit="s", description="Wall time per embed call"
)


class IndexService:
    def __init__(
        self,
        *,
        titles: TitleRepository,
        embeddings: TitleEmbeddingRepository,
        embedder: Embedder,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._titles = titles
        self._embeddings = embeddings
        # Held, never built. A loaded model is a process-lifetime resource and
        # this service is constructed once per worker *pass*
        # (`composition.build_worker`); building one here would load 65 MB of
        # ONNX every five seconds. `composition.embedder` is what makes that
        # structural.
        self._embedder = embedder
        self._commit = commit

    async def index(self, title_id: uuid.UUID) -> None:
        """Bring one title's embedding up to date.

        Raises `UsherPortError`.

        **Safe to call twice with no observable difference**, and cheap the
        second time: the stored row is compared against the current
        `(model_name, fingerprint)` before the model is asked for anything.
        Redelivery is not hypothetical -- `JobWorker.recover()` requeues a
        claim whose worker stopped heartbeating -- and at ~83 texts/s a requeued backfill
        that re-embedded would re-run the whole enriched tier.

        Re-raises rather than absorbing: `JobWorker` is the only thing that
        knows `PortDataMalformed` parks immediately and every other port error
        backs off, and it learns which by catching the exception.
        """
        with _tracer.start_as_current_span("index.title") as span:
            span.set_attribute("usher.title_id", str(title_id))
            title = await self._titles.get(title_id)
            if title is None:
                # Completed, not parked.
                logger.debug("index job names a title that no longer exists: {id}", id=title_id)
                return

            # **Site three of the document's three spellings, and the one that gets
            # missed.** `credit_names` is in `DERIVED_COLUMNS`, so `_to_domain` filters
            # it out and the `Title` above cannot supply it -- `compose_document(title)`
            # silently composes the M6 document.
            names = await self._titles.credit_names_for([title_id])
            document = compose_document(title, credits=names.get(title.id, ()))
            stored = await self._embeddings.get(title_id)
            if (
                stored is not None
                and stored.model_name == self._embedder.model_name
                and stored.source_fingerprint == document.fingerprint
            ):
                # Fingerprint, never existence. A skip on existence alone
                # passes every idempotence case and then never updates a
                # vector again -- a stale index that does not raise, it
                # answers.
                span.set_attribute("usher.index.skipped", True)
                return

            # **A refusal is a written outcome, not a skipped one.** Returning here
            # instead would leave this title matching the stale predicate forever: re-
            # claimed every backfill pass, counted on every scrape, with a handler that
            # completes successfully each time.
            vector = None if document.is_degenerate else await self._embed(document.text)
            await self._embeddings.upsert_many(
                [
                    TitleEmbeddingUpsert(
                        title_id=title.id,
                        embedding=vector,
                        model_name=self._embedder.model_name,
                        source_fingerprint=document.fingerprint,
                    )
                ]
            )
            await self._commit()
            span.set_attribute("usher.index.degenerate", document.is_degenerate)

    async def _embed(self, text: str) -> tuple[float, ...]:
        """One vector, checked before it reaches a `halfvec(384)` column."""
        with _tracer.start_as_current_span("index.embed") as span:
            span.set_attribute("usher.embedding.model", self._embedder.model_name)
            started = time.perf_counter()
            vectors = await self._embedder.embed([text])
            # Recorded before the two checks below, so a model that answers
            # wrongly still contributes the time it took to answer -- a swap
            # returning 512 floats slowly is exactly the case an operator
            # would be reading this series to understand.
            _embedding_duration.record(time.perf_counter() - started)
        if len(vectors) != 1:
            raise PortDataMalformed(
                "embedder returned the wrong number of vectors", detail=str(len(vectors))
            )
        if len(vectors[0]) != self._embedder.dimension:
            raise PortDataMalformed(
                f"embedder returned a {len(vectors[0])}-wide vector",
                detail=f"{self._embedder.model_name} declares {self._embedder.dimension}",
            )
        return tuple(vectors[0])


__all__ = ["IndexService"]
