"""Read-through: what a client gets when it opens a title (PRD 03, PRD 07).

**Nothing here calls a source, and that is the whole design.** PRD 08: "a
degraded subsystem narrows functionality; it never fails a request local
state can answer." Every fact this returns is in `titles`, `media_items` and
`watch_states`, so an unreachable Emby cannot fail a title read -- not
because the failure is caught, but because the call that could fail is never
made. `usher.ports.errors` draws the same line from the other side:
`PortUnavailable` is "distinct from *the requested thing does not exist*",
and `get_item` answers `None` for absence rather than raising.

That is also why M5 ships no RFC 9457 envelope. PRD 07's own worked example
of one is `503 source_unavailable` on `POST /titles/{id}/play` -- a request
the client made that Usher genuinely could not answer. This service cannot
produce that failure, so there is no status code to give a `code` to, and
inventing a vocabulary against a route that cannot use it is guessing. The
first route whose honest answer is "the source is down and I cannot serve
this from local state" is M9's `/play`.

**What a degraded source *does* change here is the answer's width, never its
success.** A copy the nightly sweep retracted comes back with
`available = false` rather than being filtered out (PRD 02: soft-delete
availability), so the client renders "on Living Room Emby, not currently
reported" instead of "on no source". Narrowed, not failed.

**And it promotes.** PRD 03: "Requesting an unenriched title promotes its job
to the front of the queue rather than blocking the response. The API returns
the stub immediately." This is the first caller of the enqueue clause M4
wrote, measured, and deliberately left uncalled.
"""

import uuid
from dataclasses import dataclass

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState, HdrFormat
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import (
    MediaItemRepository,
    SourceRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.telemetry import current_traceparent

# What a copy on a source that has since been deleted renders as. A
# `KeyError` here is a 500 on the screen an operator opens to find out what
# happened, for a row `ON DELETE CASCADE` is about to remove anyway.
_UNKNOWN_SOURCE = "Unknown source"


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
    ) -> None:
        self._titles = titles
        self._media_items = media_items
        self._sources = sources
        self._watch_states = watch_states
        self._queue = queue

    async def detail(self, title_id: uuid.UUID, *, user_id: uuid.UUID) -> TitleDetail | None:
        """One title, everything local about it, and a promotion if it needs
        one. `None` when no such title exists -- the route turns that into a
        404, and a raise would make the common case travel an exception path.

        Four reads, none of them per-copy: the title, its media items, the
        source list (a household has sources in the single digits, and one
        batched read serves every badge), and this user's watch state.
        """
        title = await self._titles.get(title_id)
        if title is None:
            return None
        copies = await self._media_items.list_for_title(title_id)
        names = {source.id: source.name for source in await self._sources.list_all()}
        watch_state = await self._watch_states.get_for_title(user_id, title_id)
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
            promoted=promoted,
        )

    async def _promote(self, title: Title) -> bool:
        """Move this title's enrichment to the front of the queue.

        **The comparison goes through `ENRICHMENT_RANK`**, never through the
        members. `EnrichmentState` is a `StrEnum` and compares
        lexicographically, so `title.enrichment_state < EnrichmentState.
        ENRICHED` is `False` for a `SKELETON` -- the guard would silently
        never promote anything at all, which is the exact shape ADR-0008's
        rank map exists to make unspellable.

        **A parked job stays parked.** PRD 08: "Re-enqueueing does not
        un-park... and a parked job's priority is not promoted behind their
        back either." The enqueue statement enforces it
        (`WHERE jobs.status <> 'parked'`) and nothing here works around it;
        the client is told through `Title.enrichment_error`, which PRD 07's
        wire contract carries for exactly this.

        Returns whether an enqueue was *attempted*, not whether a row
        changed. A second request for the same title writes nothing -- the
        same clause's `AND jobs.priority < excluded.priority` sees nothing
        left to promote -- and M4 measured that as the honest number rather
        than a failure to promote. **`FakeJobQueue` counts that re-enqueue as
        one row written and Postgres answers 0**, so the distinction is
        invisible to every unit case here and is pinned by
        `tests/integration/test_services_titles.py`.

        **`ENRICHMENT_RANK[state] >= ENRICHMENT_RANK[ENRICHED]` and
        `state is ENRICHED` agree on all three rungs today**, and the
        mutation between them survives the whole suite. Kept as the rank
        comparison anyway: it stops agreeing the moment a fourth tier exists,
        and it is the spelling that makes "is this already as good as it
        gets" legible rather than a coincidence of the enum having exactly
        one top. Recorded rather than silently preferred -- measured
        2026-08-01, the one survivor of seventeen.
        """
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
