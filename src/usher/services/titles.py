"""Read-through: what a client gets when it opens a title (PRD 03, PRD 07)."""

import uuid
from dataclasses import dataclass

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState, HdrFormat
from usher.domain.image import Image
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.people import CreditKind
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import (
    CreditedPerson,
    CreditRepository,
    ImageRepository,
    MediaItemRepository,
    SourceRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.services.images import servable_images
from usher.telemetry import current_traceparent

# What a copy on a source that has since been deleted renders as. A
# `KeyError` here is a 500 on the screen an operator opens to find out what
# happened, for a row `ON DELETE CASCADE` is about to remove anyway.
_UNKNOWN_SOURCE = "Unknown source"

# How many cast and how many crew a detail response carries.
CAST_LIMIT = 20
CREW_LIMIT = 20


@dataclass(frozen=True, slots=True)
class TitleAvailability:
    """One copy of a title on one source, as a client renders it.

    `source_name` rather than only `source_id`: a client renders "on Living
    Room Emby", and PRD 07's own example response carries a name. Nothing
    source-*specific* escapes here -- a name an operator typed is not an Emby
    concept, and the quality facts are already translated into Usher's own
    vocabulary by the adapter that read them (`HdrFormat`, not
    `"DolbyVision"`).

    `available` is carried rather than filtered out, because PRD 02 is
    soft-delete availability and "this is on a source that is currently not
    reporting it" is a different thing to render than "this is on no source".
    """

    source_id: uuid.UUID
    source_name: str
    external_id: str
    available: bool
    container: str | None
    video_codec: str | None
    hdr_format: HdrFormat | None
    resolution: str | None
    runtime_seconds: int | None


@dataclass(frozen=True, slots=True)
class TitleDetail:
    title: Title
    availability: tuple[TitleAvailability, ...]
    watch_state: WatchState | None
    # Top-billed first, and crew apart from cast (PRD 07's outstanding shape decision,
    # answered by M9).
    cast: tuple[CreditedPerson, ...]
    crew: tuple[CreditedPerson, ...]
    # This title's artwork in `(is_primary DESC, id)`, **already filtered** to what `GET
    # /images/{id}` can serve.
    images: tuple[Image, ...]
    # Whether this read moved an enrichment job to the front of the queue.
    # Returned rather than kept internal because it is what makes the
    # read-through path observable end to end -- PRD 10's "promotion latency
    # against the 5 s read-through target" needs something to start from.
    promoted: bool


class TitleReadService:
    def __init__(
        self,
        titles: TitleRepository,
        media_items: MediaItemRepository,
        sources: SourceRepository,
        watch_states: WatchStateRepository,
        queue: JobQueue,
        credits: CreditRepository,
        images: ImageRepository,
    ) -> None:
        self._titles = titles
        self._media_items = media_items
        self._sources = sources
        self._watch_states = watch_states
        self._queue = queue
        self._credits = credits
        self._images = images

    async def detail(self, title_id: uuid.UUID, *, user_id: uuid.UUID) -> TitleDetail | None:
        """One title, everything local about it, and a promotion if it needs one.

        `None` when no such title exists -- the route turns that into a 404, and a raise
        would make the common case travel an exception path.
        """
        title = await self._titles.get(title_id)
        if title is None:
            return None
        copies = await self._media_items.list_for_title(title_id)
        names = {source.id: source.name for source in await self._sources.list_all()}
        watch_state = await self._watch_states.get_for_title(user_id, title_id)
        cast = await self._credits.list_for_title(title_id, kind=CreditKind.CAST, limit=CAST_LIMIT)
        crew = await self._credits.list_for_title(title_id, kind=CreditKind.CREW, limit=CREW_LIMIT)
        images = servable_images(await self._images.list_for_title(title_id))
        promoted = await self._promote(title)
        return TitleDetail(
            title=title,
            availability=tuple(
                TitleAvailability(
                    source_id=copy.source_id,
                    # `.get`, never `names[...]`: `media_items.source_id` is
                    # `ON DELETE CASCADE`, so a source removed between the
                    # two reads leaves a copy naming a row that is already
                    # gone. "Unknown source" is a better answer than a 500.
                    source_name=names.get(copy.source_id, _UNKNOWN_SOURCE),
                    external_id=copy.external_id,
                    available=copy.available,
                    container=copy.container,
                    video_codec=copy.video_codec,
                    hdr_format=copy.hdr_format,
                    resolution=(
                        f"{copy.width}x{copy.height}"
                        if copy.width is not None and copy.height is not None
                        else None
                    ),
                    runtime_seconds=copy.runtime_seconds,
                )
                for copy in copies
            ),
            watch_state=watch_state,
            cast=tuple(cast),
            crew=tuple(crew),
            images=images,
            promoted=promoted,
        )

    async def _promote(self, title: Title) -> bool:
        """Move this title's enrichment to the front of the queue."""
        if ENRICHMENT_RANK[title.enrichment_state] >= ENRICHMENT_RANK[EnrichmentState.ENRICHED]:
            return False
        await self._queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.ENRICH,
                    key=str(title.id),
                    priority=JobPriority.DEMAND,
                    # PRD 10's "why did the title I just opened take 45
                    # seconds": the worker's span links back to this
                    # request's, minutes later.
                    traceparent=current_traceparent(),
                )
            ]
        )
        return True
