"""PRD 03's demand lane, for the screens rather than for one title."""

import uuid
from collections.abc import Iterable, Sequence

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.rows import RowCard
from usher.domain.title import Title
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import TitleRepository
from usher.telemetry import current_traceparent

_FINISHED = ENRICHMENT_RANK[EnrichmentState.ENRICHED]


class VisibilityService:
    """Promote the skeletons a client was just shown."""

    def __init__(self, queue: JobQueue, titles: TitleRepository) -> None:
        # The *same* queue the worker claims from, which only a composition
        # root can know -- `EnrichService` takes its own for this reason and
        # says so. A defaulted queue here is how a screen ends up promoting
        # into an object nothing ever claims from, on the one path whose
        # failure mode is that nothing visibly happens.
        self._queue = queue
        # Only `seen_ids` reads this. Required rather than optional because an
        # optional repository makes "this deployment cannot resolve ids" and
        # "these ids are all enriched" the same answer -- zero -- on the surface
        # with the strongest intent signal.
        self._titles = titles

    async def seen(self, titles: Iterable[Title]) -> int:
        """Enqueue one `enrich` at `VISIBLE` per unfinished title.

        Returns how many were promoted.

        The guard is `ENRICHMENT_RANK`, never `state is SKELETON`: there are
        three rungs and the direct spelling strands every `stub` on a screen
        forever. It is also never a `>` comparison on the enum itself --
        `EnrichmentState` is a `StrEnum`, so `ENRICHED > SKELETON` is `False` and
        a guard spelled that way promotes nothing at all, silently.

        Deduplicated because one title can sit on two shelves of one composed
        screen, and the count returned is read as "titles promoted".
        """
        return await self._promote((title.id, title.enrichment_state) for title in titles)

    async def seen_ids(self, title_ids: Sequence[uuid.UUID]) -> int:
        """`seen`, for a surface that never held a `Title`.

        `GET /search` is the case this exists for: `SearchResult` carries
        `title_id` and no `enrichment_state`, so the surface with
        the strongest intent signal in the API cannot answer "is this a
        skeleton" from what it already has. One `WHERE id = ANY(...)` on the
        primary key, bounded by the caller's own result limit.

        Empty in, nothing read. A query that matched nothing is the ordinary
        answer, and the read is as much a per-request cost as the write the guard
        in `_promote` already covers.

        An id the catalog no longer holds is simply absent from the answer --
        `list_by_ids` promises only what it holds, because a title deleted
        between an index write and a search read is ordinary. Dropped rather
        than raised: a search that 500s over one stale hit is a worse failure
        than the stale hit.
        """
        if not title_ids:
            return 0
        return await self.seen(await self._titles.list_by_ids(list(title_ids)))

    async def seen_cards(self, cards: Iterable[RowCard]) -> int:
        """`seen`, for the composed screen.

        `RowCard` carries its own `enrichment_state` -- unlike `SearchResult`,
        which is the whole reason `seen_ids` exists -- so a screen is judged from
        what the composer already hydrated rather than re-read. Nine shelves of
        up to twenty cards would otherwise be a second read of the entire screen
        for a field it is holding.

        Called once for the whole screen rather than once per provider, which
        is what makes the dedup in `_promote` load-bearing: nothing stops a
        film being both recently-added and a genre affinity, and without it
        one title on two shelves is two rows COPYed and one discarded.
        """
        return await self._promote((card.title_id, card.enrichment_state) for card in cards)

    async def _promote(self, tiers: Iterable[tuple[uuid.UUID, EnrichmentState]]) -> int:
        unfinished: dict[str, None] = {}
        for title_id, state in tiers:
            if ENRICHMENT_RANK[state] < _FINISHED:
                unfinished[str(title_id)] = None
        if not unfinished:
            return 0
        traceparent = current_traceparent()
        await self._queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.ENRICH,
                    key=key,
                    priority=JobPriority.VISIBLE,
                    traceparent=traceparent,
                )
                for key in unfinished
            ]
        )
        return len(unfinished)
