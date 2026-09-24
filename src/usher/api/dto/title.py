"""`GET /titles/{id}` (PRD 07)."""

import uuid

from pydantic import AwareDatetime, BaseModel

from usher.domain.enums import EnrichmentState, HdrFormat, ImageKind, TitleKind
from usher.domain.image import Image
from usher.ports.repository import CreditedPerson
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
    """One badge.

    Present whether or not the copy is currently available.
    """

    source_id: uuid.UUID
    # `source`, not `source_name`: PRD 07's own example spells it this way,
    # and this is a wire contract rather than a rename of a domain field.
    source: str
    # PRD 02 is "soft-delete availability, hard-delete nothing", so a copy the nightly
    # sweep retracted is rendered with `false` rather than dropped -- a client that
    # showed "not on any source" for a film on a temporarily unmounted drive would be
    # stating a different fact than the one stored.
    available: bool
    container: str | None
    video_codec: str | None
    hdr_format: HdrFormat | None
    # `null` rather than "NonexNone": an Emby `Series` item has no `MediaSource`
    # and therefore no dimensions at all.
    resolution: str | None
    runtime_seconds: int | None


class CreditResponse(BaseModel):
    """One person's involvement in this title, as a client renders it."""

    person_id: uuid.UUID
    name: str
    character: str | None
    job: str | None

    @classmethod
    def of(cls, credit: CreditedPerson) -> "CreditResponse":
        return cls(
            person_id=credit.person_id,
            name=credit.name,
            character=credit.character,
            job=credit.job,
        )


class ImageResponse(BaseModel):
    """One artwork reference: an id to fetch and what it is a picture of."""

    id: uuid.UUID
    kind: ImageKind

    @classmethod
    def of(cls, image: Image) -> "ImageResponse":
        return cls(id=image.id, kind=image.kind)


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
    # A *separate, independent* field: the wire contract carries no `failed` tier,
    # because a skeleton whose enrichment failed is still a usable skeleton. It is
    # also how a *parked* enrichment reaches the client -- PRD 08 forbids
    # un-parking it behind a human's back, so the honest answer is to say so.
    enrichment_error: str | None
    availability: list[AvailabilityResponse]
    watch_state: WatchStateResponse | None
    # **Absent when empty, and never `[]` or `null`.** The mechanism is the route's
    # `response_model_exclude_unset=True` plus an `of` that does not *set* an empty one
    # -- so the default here is the empty tuple rather than `None`, and the declared
    # type admits no null.
    cast: tuple[CreditResponse, ...] = ()
    crew: tuple[CreditResponse, ...] = ()
    # Same mechanism, same default, same declared type: a title with no artwork --
    # or none this proxy can serve -- carries no `images` key. PRD 07's convention
    # is absence, not `[]`.
    images: tuple[ImageResponse, ...] = ()

    @classmethod
    def of(cls, detail: TitleDetail) -> "TitleResponse":
        # Set only when there is something to say. `cls(cast=(), ...)` and
        # omitting the argument build equal objects and *different* responses,
        # which is the one thing about this class worth reading twice.
        optional: dict[str, tuple[CreditResponse, ...] | tuple[ImageResponse, ...]] = {}
        if detail.cast:
            optional["cast"] = tuple(CreditResponse.of(one) for one in detail.cast)
        if detail.crew:
            optional["crew"] = tuple(CreditResponse.of(one) for one in detail.crew)
        if detail.images:
            optional["images"] = tuple(ImageResponse.of(one) for one in detail.images)
        return cls(
            **optional,
            id=detail.title.id,
            kind=detail.title.kind,
            name=detail.title.name,
            year=detail.title.year,
            overview=detail.title.overview,
            tagline=detail.title.tagline,
            runtime_minutes=detail.title.runtime_minutes,
            genres=detail.title.genres,
            community_rating=detail.title.tmdb_vote_average,
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
