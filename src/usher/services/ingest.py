"""PRD 03 stage 1: one page of a walk, into canonical state.

Deliberately owns no walk and no run. `ReconcileService` drives the adapter,
batches, checkpoints, and decides whether the availability sweep may run;
this owns what happens to one batch once it exists. The split is what makes
every case in `tests/unit/test_services_ingest.py` runnable with no adapter
at all.

**`observed_at` is the run's start instant, passed in, never `now()`.** The
availability sweep retracts everything with `last_seen_at < run.started_at`,
so `last_seen_at` has to mean "the run that saw this item" rather than "when
this row happened to be written" -- the two are only ever equal by accident,
and nothing downstream can reconstruct the first from the second. It is a
parameter rather than a default for that reason.

**Episodes are attached, never invented, and never matched.** An episode's
canonical parent is its series' `Title`. `MatchService` refuses to run one
through the ladder at all (its own docstring says why: the ids a source
reports for an episode are the *episode's*, and 999,827 junk titles is the
cost of pretending otherwise), so every episode arrives here `UNMATCHED` and
leaves either attached to its series or still unmatched. Neither outcome
drops it -- PRD 02's "unmatched items are never dropped", applied to the 89%
of this library that is episodes.

**Every repository call is once per batch.** Two provider/name lookups and
one enrichment-state read through `TitleMatchRepository`, one
`resolve_series_titles`, one `upsert_many`, four episode calls, and exactly
one `enqueue` carrying every follow-up job the batch produced. The per-item
spelling of any of them is 999,827 round trips a walk.
"""

import uuid
from collections.abc import Sequence

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState, MatchMethod
from usher.domain.episode import Episode, Season
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.ingest import IngestResult, MatchOutcome, MediaItemUpsert
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import (
    EpisodeRepository,
    MediaItemRepository,
    TitleMatchRepository,
)
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.matching import MatchService
from usher.telemetry import current_traceparent

_tracer = trace.get_tracer("usher.ingest")
_meter = metrics.get_meter("usher.ingest")
_items_counter = _meter.create_counter(
    "usher.ingest.items", unit="1", description="Source items ingested, by result"
)

_ENRICHED_RANK = ENRICHMENT_RANK[EnrichmentState.ENRICHED]

# `(title_id, season_number)` and `(title_id, season_number, episode_number)`
# -- the natural keys `EpisodeRepository` resolves on. Named because they are
# what makes two shows' S01E01 two rows: 32,409 series means a key without the
# title is a collision, not a risk.
_SeasonKey = tuple[uuid.UUID, int]
_EpisodeKey = tuple[uuid.UUID, int, int]


class IngestService:
    def __init__(
        self,
        matcher: MatchService,
        matching: TitleMatchRepository,
        media_items: MediaItemRepository,
        episodes: EpisodeRepository,
        queue: JobQueue,
    ) -> None:
        self._matcher = matcher
        self._matching = matching
        self._media_items = media_items
        self._episodes = episodes
        self._queue = queue

    async def ingest_batch(
        self, source_id: uuid.UUID, items: Sequence[SourceItem], *, observed_at: AwareDatetime
    ) -> IngestResult:
        """Match, store, attach episodes, enqueue follow-up work. Idempotent."""
        with _tracer.start_as_current_span("ingest.item") as span:
            span.set_attribute("usher.source_id", str(source_id))
            span.set_attribute("usher.batch.items", len(items))
            if not items:
                # A walk's last page is routinely empty, and so is a delta
                # walk that found nothing. Neither is worth a statement.
                return IngestResult(inserted=0, updated=0, matched=0, unmatched=0)
            outcomes = {
                outcome.external_id: outcome for outcome in await self._matcher.match(items)
            }
            outcomes = await self._attach_episodes(source_id, items, outcomes)
            ordered = tuple(outcomes[item.external_id] for item in items)
            result = await self._media_items.upsert_many(
                [
                    self._upsert_for(source_id, item, outcomes[item.external_id], observed_at)
                    for item in items
                ]
            )
            await self._enqueue_followups(items, outcomes)
            for outcome in ordered:
                _items_counter.add(1, {"source": str(source_id), "result": outcome.method.value})
            matched = sum(1 for outcome in ordered if outcome.title_id is not None)
            span.set_attribute("usher.inserted", result.inserted)
            span.set_attribute("usher.updated", result.updated)
            span.set_attribute("usher.matched", matched)
            return IngestResult(
                inserted=result.inserted,
                updated=result.updated,
                matched=matched,
                unmatched=len(ordered) - matched,
                outcomes=ordered,
            )

    async def _attach_episodes(
        self,
        source_id: uuid.UUID,
        items: Sequence[SourceItem],
        outcomes: dict[str, MatchOutcome],
    ) -> dict[str, MatchOutcome]:
        """Resolve each episode item's series, create its season and episode
        rows, and rewrite its outcome to carry both ids.

        Four writes and one read for the whole batch, never per episode. The
        per-episode spelling reads more clearly and is a scale defect: at
        999,827 episodes and three round trips apiece it is the difference
        between a walk that finishes and one that does not.
        """
        episodes = [item for item in items if item.kind is SourceItemKind.EPISODE]
        if not episodes:
            return outcomes
        series_titles = await self._series_titles(source_id, items, outcomes, episodes)

        # Everything resolvable, paired with the title it hangs off. An
        # episode with no series, no title for its series, or no numbers is
        # simply absent -- it keeps its UNMATCHED outcome and gets a re-match
        # job. Attaching on a defaulted number would hang every one of them
        # off S00E00 and collapse them into a single row.
        attachable: list[tuple[SourceItem, uuid.UUID]] = []
        for item in episodes:
            title_id = series_titles.get(item.series_external_id or "")
            if title_id is None or item.season_number is None or item.episode_number is None:
                continue
            attachable.append((item, title_id))
        if not attachable:
            return outcomes

        season_ids = await self._ensure_seasons(attachable)
        episode_ids = await self._ensure_episodes(attachable, season_ids)

        resolved = dict(outcomes)
        for item, title_id in attachable:
            key = (title_id, item.season_number or 0, item.episode_number or 0)
            episode_id = episode_ids.get(key)
            if episode_id is None:
                # The upsert wrote it and the resolve did not find it. Not
                # reachable through either implementation, and left unattached
                # rather than asserted on: a walk that raises here retracts
                # nothing but costs the run, and an item in the review queue
                # is recoverable.
                logger.warning(
                    "episode {external_id} was written but did not resolve; leaving it unmatched",
                    external_id=item.external_id,
                )
                continue
            resolved[item.external_id] = MatchOutcome(
                external_id=item.external_id,
                title_id=title_id,
                method=MatchMethod.SERIES_PARENT,
                episode_id=episode_id,
            )
        return resolved

    async def _series_titles(
        self,
        source_id: uuid.UUID,
        items: Sequence[SourceItem],
        outcomes: dict[str, MatchOutcome],
        episodes: Sequence[SourceItem],
    ) -> dict[str, uuid.UUID]:
        """`series_external_id` -> `title_id`, from this batch and the store.

        The in-batch half is built from the *whole* page rather than from the
        items already processed: `SortBy=DateCreated` says nothing about a
        series preceding its own episodes, and Emby genuinely interleaves
        them, so an implementation that accumulated as it went would attach on
        one ordering and silently miss on the other.

        The stored half is one `resolve_series_titles` call, for the series
        this page does not carry -- an episode whose series arrived three
        pages ago is the normal case, not the exception.
        """
        in_batch = {
            item.external_id: outcomes[item.external_id].title_id
            for item in items
            if item.kind is SourceItemKind.SERIES
            and outcomes[item.external_id].title_id is not None
        }
        wanted = [
            item.series_external_id
            for item in episodes
            if item.series_external_id and item.series_external_id not in in_batch
        ]
        stored = await self._media_items.resolve_series_titles(source_id, wanted)
        return {**stored, **{k: v for k, v in in_batch.items() if v is not None}}

    async def _ensure_seasons(
        self, attachable: Sequence[tuple[SourceItem, uuid.UUID]]
    ) -> dict[_SeasonKey, uuid.UUID]:
        """One `upsert_seasons` and one `resolve_seasons` for the page.

        The resolve is not redundant with the upsert: ingest mints a fresh
        UUIDv7 per sighting, and a season the catalog already holds keeps the
        id it was inserted with -- so the id an episode's `season_id` must
        carry is knowable only by reading it back.
        """
        wanted = {(title_id, item.season_number or 0) for item, title_id in attachable}
        await self._episodes.upsert_seasons(
            [Season(title_id=title_id, season_number=number) for title_id, number in wanted]
        )
        return await self._episodes.resolve_seasons(list(wanted))

    async def _ensure_episodes(
        self,
        attachable: Sequence[tuple[SourceItem, uuid.UUID]],
        season_ids: dict[_SeasonKey, uuid.UUID],
    ) -> dict[_EpisodeKey, uuid.UUID]:
        rows = [
            Episode(
                title_id=title_id,
                season_id=season_id,
                season_number=item.season_number or 0,
                episode_number=item.episode_number or 0,
                # The name is the only thing a source reliably has that
                # enrichment will later improve on. `upsert_episodes` never
                # blanks a non-null field with a null one, so a nightly walk
                # cannot undo an enriched overview.
                name=item.name or None,
            )
            for item, title_id in attachable
            if (season_id := season_ids.get((title_id, item.season_number or 0))) is not None
        ]
        if not rows:
            return {}
        await self._episodes.upsert_episodes(rows)
        return await self._episodes.resolve_episodes(
            [(row.title_id, row.season_number, row.episode_number) for row in rows]
        )

    def _upsert_for(
        self,
        source_id: uuid.UUID,
        item: SourceItem,
        outcome: MatchOutcome,
        observed_at: AwareDatetime,
    ) -> MediaItemUpsert:
        return MediaItemUpsert(
            source_id=source_id,
            external_id=item.external_id,
            title_id=outcome.title_id,
            episode_id=outcome.episode_id,
            container=item.container,
            video_codec=item.video_codec,
            audio_codec=item.audio_codec,
            width=item.width,
            height=item.height,
            hdr_format=item.hdr_format,
            audio_channels=item.audio_channels,
            file_size_bytes=item.file_size_bytes,
            runtime_seconds=item.runtime_seconds,
            added_at=item.added_at,
            last_seen_at=observed_at,
        )

    async def _enqueue_followups(
        self, items: Sequence[SourceItem], outcomes: dict[str, MatchOutcome]
    ) -> None:
        """Every job this batch produced, in one `enqueue`.

        Two populations, and they are deliberately one statement: enrichment
        for the titles this walk touched that are not already enriched, and a
        re-match for the episodes whose series was not yet known.
        `MatchService` has already enqueued a `match` job for every *other*
        unmatched item, so the two callers between them issue two enqueues per
        batch and never overlap.
        """
        traceparent = current_traceparent()
        requests = [
            JobRequest(
                kind=JobKind.ENRICH,
                key=str(title_id),
                priority=JobPriority.NEW,
                traceparent=traceparent,
            )
            for title_id in await self._titles_needing_enrichment(outcomes)
        ]
        requests.extend(
            JobRequest(
                kind=JobKind.MATCH,
                key=item.external_id,
                priority=JobPriority.BACKFILL,
                traceparent=traceparent,
            )
            for item in items
            if item.kind is SourceItemKind.EPISODE and outcomes[item.external_id].title_id is None
        )
        if requests:
            await self._queue.enqueue(requests)

    async def _titles_needing_enrichment(
        self, outcomes: dict[str, MatchOutcome]
    ) -> list[uuid.UUID]:
        """The titles this batch touched that are not already `enriched`.

        A nightly walk sees all 1,126,674 items every night. Enqueueing
        enrichment for each one makes the queue permanently the size of the
        library and starves every demand-promoted job behind it -- and the
        `(kind, key)` uniqueness would collapse them to one per title, which
        merely turns "1.1M inserts" into "1.1M no-op upserts" every night.

        One read for the whole batch, stubs included. Short-circuiting the
        titles this batch just stubbed looks free -- `CREATED_STUB` sounds
        like a claim that the row is new -- and is not: that method also
        covers an item that *lost* the create race and attached to an
        existing title, which may already be enriched. Asking about every id
        costs nothing (they are already in one statement) and is right in
        both cases. It is also readable: `TitleRepository.add` flushes, so a
        stub the match stage wrote a moment ago is visible to this read.

        `ENRICHMENT_RANK`, never a direct comparison: `EnrichmentState` is a
        `StrEnum` and `ENRICHED > SKELETON` is `False` (ADR-0008), so
        `state >= ENRICHED` is `True` for `"stub"` and would skip precisely
        the tier most in need of enrichment.
        """
        title_ids = list(
            {outcome.title_id for outcome in outcomes.values() if outcome.title_id is not None}
        )
        if not title_ids:
            return []
        states = await self._matching.enrichment_states(title_ids)
        return [
            title_id
            for title_id in title_ids
            # Absent means the title is gone. Enqueueing enrichment for a row
            # that does not exist buys a parked job and a confused operator.
            if (state := states.get(title_id)) is not None
            and ENRICHMENT_RANK[state] < _ENRICHED_RANK
        ]
