"""PRD 03 stage 4's queued half: one title, one vector, one fingerprint.

**Only half of stage 4 is here, and the asymmetry is the milestone's central
decision.** The full-text document is a `GENERATED ALWAYS AS (...) STORED`
column on `titles`, so no code path -- including a bulk `COPY`, a hand-written
`UPDATE` or a future migration -- can write a title and skip its document. The
embedding needs a model, so it is a job; and jobs can fail, park, or never be
enqueued. Rebuilding the document here alongside the embedding is the obvious
symmetry and is wrong: it makes the cheap, always-correct half depend on the
expensive, fallible one, and a parked embedding job would then also mean a stale
full-text document with the two failures indistinguishable.

So this service does not trust the queue. Every row it writes records *what* was
embedded (`source_fingerprint`) and *by what* (`model_name`), which makes
staleness a predicate rather than an inference.

`commit` is injected because `services/` may depend only on `domain/` and
`ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), and a
session is neither.
"""

import uuid
from collections.abc import Awaitable, Callable

from loguru import logger
from opentelemetry import trace

from usher.ports.embedding import Embedder
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import (
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
    TitleRepository,
)
from usher.services.search import compose_document

_tracer = trace.get_tracer("usher.index")


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
        """Bring one title's embedding up to date. Raises `UsherPortError`.

        **Safe to call twice with no observable difference**, and cheap the
        second time: the stored row is compared against the current
        `(model_name, fingerprint)` before the model is asked for anything.
        Redelivery is not hypothetical -- `JobWorker.startup()` requeues
        everything left `running` -- and at ~83 texts/s a requeued backfill
        that re-embedded would re-run the whole enriched tier.

        Re-raises rather than absorbing: `JobWorker` is the only thing that
        knows `PortDataMalformed` parks immediately and every other port error
        backs off, and it learns which by catching the exception.
        """
        with _tracer.start_as_current_span("index.title") as span:
            span.set_attribute("usher.title_id", str(title_id))
            title = await self._titles.get(title_id)
            if title is None:
                # Completed, not parked. `handlers.py`: "a job for work that
                # has since become impossible completes rather than parks".
                # The backfill enqueues over the whole enriched tier, so a
                # title deleted between the sweep and the claim is ordinary.
                # `EnrichService` parks here and is right to -- its keys come
                # from a walk that just saw the item.
                logger.debug("index job names a title that no longer exists: {id}", id=title_id)
                return

            document = compose_document(title)
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

            # **A refusal is a written outcome, not a skipped one.** Returning
            # here instead would leave this title matching the stale predicate
            # forever: re-claimed every backfill pass, counted on every
            # scrape, with a handler that completes successfully each time.
            # The watch-history repair shipped exactly that shape once. The
            # row below carries the fingerprint of the degenerate text, so the
            # title stops matching the stale predicate and starts matching the
            # `embedding IS NULL` one a diagnostic counts -- and the moment
            # enrichment gives it an overview the fingerprint moves and it is
            # re-claimed exactly once.
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
        """One vector, checked before it reaches a `halfvec(384)` column.

        Both checks are `PortDataMalformed` rather than retryable: a model
        that returns two vectors for one text, or 512 floats where 384 were
        declared, answers the same way however many times it is asked. Left to
        the database, the second surfaces one statement later as a constraint
        error naming a *column*, on a job whose real problem is a model swap.
        """
        vectors = await self._embedder.embed([text])
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
