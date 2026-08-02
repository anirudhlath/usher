"""`GET /titles/{id}` (PRD 07).

**Four fields PRD 07's example carries are absent**, and each is deferred to
the milestone that fills it rather than shipped empty: `images` (M9's proxy),
`credits` (M7 derives `Person`/`Credit` from `raw_payloads` with no second
network call), `similar` (M6's neighbours, and its own route), and the
season/episode hierarchy (M9's `GET /series/{id}/seasons`). PRD 09's boundary
call 2 assigns all four. An empty list would be worse than an absent field: a
client cannot tell "not derived yet" from "this film has no cast", which is
the response-shaped version of the empty-dashboard-panel problem.

**No `external_id` on the wire.** PRD 07's first line is "Nothing in this
surface mentions a media server", and a source's own item id is both a source
concept escaping its adapter and a value no client has a use for -- every
route a client calls takes a `Title.id`. The source *name* is a different
thing and is present: it is what an operator typed, and PRD 07's own example
badge carries one.

**And no credential, structurally.** `TitleAvailability` carries no
`base_url` and no `credentials_ref`, so there is no field here for one to
land in -- the same argument `api/dto/source.py` makes about
`SourceResponse`. ADR-0012's one documented exception is a `direct` playback
target's URL, which belongs to M9's `POST /titles/{id}/play`.
"""

import uuid

from pydantic import AwareDatetime, BaseModel

from usher.domain.enums import EnrichmentState, HdrFormat, TitleKind
from usher.services.titles import TitleDetail


class WatchStateResponse(BaseModel):
    """Progress, or `null` -- never a fabricated all-zero record.

    PRD 07's "render deliberately rather than inferring intent from nulls"
    runs the other way here: `{position_seconds: 0, played: false}` is a real
    state ("started and abandoned at second zero") and a client has to be able
    to tell it from "this user has never touched this title".
    """

    position_seconds: int
    played: bool
    play_count: int
    last_played_at: AwareDatetime | None


class AvailabilityResponse(BaseModel):
    """One badge. Present whether or not the copy is currently available."""

    source_id: uuid.UUID
    # `source`, not `source_name`: PRD 07's own example spells it this way,
    # and this is a wire contract rather than a rename of a domain field.
    source: str
    # PRD 02 is "soft-delete availability, hard-delete nothing", so a copy the
    # nightly sweep retracted is rendered with `false` rather than dropped --
    # a client that showed "not on any source" for a film on a temporarily
    # unmounted drive would be stating a different fact than the one stored.
    # This is also the only place a *degraded source* is visible on this
    # route, and it narrows the answer rather than failing it (PRD 08).
    available: bool
    container: str | None
    video_codec: str | None
    hdr_format: HdrFormat | None
    # `null` rather than "NonexNone": an Emby `Series` item has no
    # `MediaSource` and therefore no dimensions at all -- 20 of the 601 rows
    # M4's live run ingested.
    resolution: str | None
    runtime_seconds: int | None


class TitleResponse(BaseModel):
    id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    overview: str | None
    tagline: str | None
    runtime_minutes: int | None
    genres: tuple[str, ...]
    community_rating: float | None
    # PRD 07: "Every title-bearing response carries `enrichment_state` so
    # clients render deliberately -- skeleton shimmer on fields known to be
    # missing -- rather than inferring intent from nulls."
    enrichment_state: EnrichmentState
    # And a *separate, independent* field (ADR-0008): the wire contract does
    # not carry a `failed` tier, because a skeleton whose enrichment failed is
    # still a perfectly usable skeleton. It is also how a *parked* enrichment
    # reaches the client -- PRD 08 forbids un-parking it behind a human's
    # back, so the honest answer is to say what happened.
    enrichment_error: str | None
    availability: list[AvailabilityResponse]
    watch_state: WatchStateResponse | None

    @classmethod
    def of(cls, detail: TitleDetail) -> "TitleResponse":
        return cls(
            id=detail.title.id,
            kind=detail.title.kind,
            name=detail.title.name,
            year=detail.title.year,
            overview=detail.title.overview,
            tagline=detail.title.tagline,
            runtime_minutes=detail.title.runtime_minutes,
            genres=detail.title.genres,
            community_rating=detail.title.community_rating,
            enrichment_state=detail.title.enrichment_state,
            enrichment_error=detail.title.enrichment_error,
            availability=[
                AvailabilityResponse(
                    source_id=copy.source_id,
                    source=copy.source_name,
                    available=copy.available,
                    container=copy.container,
                    video_codec=copy.video_codec,
                    hdr_format=copy.hdr_format,
                    resolution=copy.resolution,
                    runtime_seconds=copy.runtime_seconds,
                )
                for copy in detail.availability
            ],
            watch_state=(
                None
                if detail.watch_state is None
                else WatchStateResponse(
                    position_seconds=detail.watch_state.position_seconds,
                    played=detail.watch_state.played,
                    play_count=detail.watch_state.play_count,
                    last_played_at=detail.watch_state.last_played_at,
                )
            ),
        )
