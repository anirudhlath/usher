"""A title page's similar shelf: its stored row or its neighbours, and the build it asks for."""

import uuid

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.rows import SimilarShelf, SimilarSource
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.rows import RowContext
from usher.services.rows.because_you_watched import FALLBACK_CARDS, BecauseYouWatchedRow
from usher.services.seed_rows import is_due
from usher.telemetry import current_traceparent


class SimilarShelfService:
    """One read per title page. Never commits; the request's session does.

    `recipe` is `None` for a deployment with no model, which never queues a build.
    """

    def __init__(self, *, ctx: RowContext, queue: JobQueue, recipe: str | None) -> None:
        self._ctx = ctx
        self._queue = queue
        self._recipe = recipe

    async def read(self, title_id: uuid.UUID) -> SimilarShelf | None:
        ctx = self._ctx
        title = await ctx.titles.get(title_id)
        if title is None:
            return None
        record = (await ctx.seed_rows.list_for([title.id])).get(title.id)
        if record is not None and record.cards:
            source = SimilarSource.ROW
            ids = [card.title_id for card in record.cards]
        else:
            source = SimilarSource.NEIGHBORS
            neighbours = await ctx.neighbors.list_for(title.id, limit=FALLBACK_CARDS)
            ids = [one.neighbor_title_id for one in neighbours]
        played = await ctx.watch_states.played_title_ids(ctx.user.id, ids) if ids else frozenset()
        built = await BecauseYouWatchedRow(
            title.id, title.name, ids, semantic=True, played=frozenset(played)
        ).build(ctx)

        enriched = (
            ENRICHMENT_RANK[title.enrichment_state] >= ENRICHMENT_RANK[EnrichmentState.ENRICHED]
        )
        building = (
            self._recipe is not None
            and enriched
            and is_due(record, recipe=self._recipe, now=ctx.now())
        )
        if building:
            await self._queue.enqueue(
                [
                    JobRequest(
                        kind=JobKind.SEED_ROW,
                        key=str(title.id),
                        priority=JobPriority.DEMAND,
                        traceparent=current_traceparent(),
                    )
                ]
            )
        return SimilarShelf(cards=built.cards, source=source, building=building)


__all__ = ["SimilarShelfService"]
