"""`GET /titles/{id}` (PRD 07).

**Four fields PRD 07's example carries were absent from M5 to M9, and M9
answered all four -- two by filling this response, two by building a route.**
This paragraph said so in the future tense for four milestones, naming each
absence and the milestone that owed it. It is rewritten **once and whole**,
here, from the tree as it stands, because four M9 tasks made it false in four
different ways and a paragraph corrected a clause at a time is a paragraph
whose last clause is always wrong. As the tree stands:

- **`credits` is two keys**, `cast` and `crew` below, each capped at 20 and
  ordered `billing_order` ascending with unbilled credits last. That was
  PRD 07's outstanding shape decision and M9 answered it; the read is a table
  `usher derive` fills from `raw_payloads` with no second network call.
- **`images` is a list of ids and kinds**, `ImageResponse` below, in the
  stored `(is_primary DESC, id)`. A client composes `GET /images/{id}?w=`
  from an id, which is what makes PRD 07's *"clients never see provider image
  URLs and never need a provider key"* a property of this body rather than
  only of the proxy.
- **`similar` is its own route**, `GET /titles/{title_id}/similar`. It reads
  M6's precomputed `title_neighbors` and carries two staleness signals this
  response has nowhere to put; a neighbour list is also a different resource,
  refreshed on a different schedule, from the title it hangs off.
- **The season/episode hierarchy is two routes**, `GET /series/{id}/seasons`
  and `GET /seasons/{id}/episodes`. One measured series holds 20,000
  episodes, so inlining the tree would make the *length* of a title response
  a property of the show rather than of the request.

**The absence rule survives all four, and it is what this file is really
about.** An empty list is worse than an absent field -- a client cannot tell
"not derived yet" from "this film has no cast", which is the response-shaped
version of the empty-dashboard-panel problem -- and **that argument does not
expire when the table lands.** A title with no artwork answers with **no
`images` key**, never `"images": []`, exactly as a title with no derived
credits answers with neither `cast` nor `crew`. What changed with the tables
is the *meaning*: absence said "this milestone has not built it" and now says
"this title has nothing here".

⚠️ **And for both filled keys, absence is ambiguous in a way nothing on the
wire resolves.** Two recorded residuals rather than two fixes:

- A title can carry `titles.credit_names` and no `Credit` rows at all -- the
  IMDb principals loader fills that column for ~93.8% of the catalog without
  deriving a single credit -- so *underived* is the ordinary case, not the
  corner, and it is indistinguishable here from a genuinely uncredited film.
  Closing it is a per-title derived-at column, a migration and a writer; a
  fabricated `credits_derived` flag that nothing sets would be worse than the
  honest silence.
- A title whose artwork this proxy declines -- an SVG logo, roughly one title
  in seventeen -- answers identically to a title with no artwork at all,
  because `TitleReadService` filters those rows out rather than annotating
  them (`usher.ports.images.is_servable_path` records the decision and its
  alternative). Unlike the credits residual this one **is** reported, just not
  on the wire: `usher.images.references` counts every read's references
  `served` against `unservable`, which is the whole reason that instrument
  exists.

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


class CreditResponse(BaseModel):
    """One person's involvement in this title, as a client renders it.

    **Four fields, and the two that are missing are missing on purpose.**
    `CreditedPerson` also carries `department` and `billing_order`.

    `billing_order` *is* the list order, already spent by the time a client
    sees this: handing it over as a field invites a client-side re-sort, and
    the tempting spelling of that (`billing_order or 0`) puts an unbilled crew
    member above the lead -- the exact defect `ORDER BY billing_order ASC
    NULLS LAST` exists to prevent, relocated into a client nobody here can
    fix. `department` is a coarser grouping than the shape decision PRD 07
    records for this route uses, and adding it later is additive; removing a
    field a client has started rendering is not.

    `character` and `job` are both nullable rather than one being absent per
    kind, because a cast entry with no character and a crew entry with no job
    are both real stored rows -- `Credit`'s own docstring: "a crew entry with
    no `job` and a cast entry with no `character` are the same row shape".
    `null` says "this row does not carry one"; an absent key would say
    something about the *kind*, which `cast`/`crew` already say.

    No `tmdb_id` and no `tmdb_credit_id`: identity in this contract is Usher's
    own UUIDv7 (ADR-0003), and a provider's id for a credit is a derivation
    detail with no client use.
    """

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
    """One artwork reference: an id to fetch and what it is a picture of.

    **Two fields, and every other column of `Image` is deliberately not one.**

    `provider` and `provider_path` are the whole of what a client would need
    to go around this API to the CDN, which is exactly what PRD 07's
    *"clients never see provider image URLs and never need a provider key"*
    forbids -- and `provider_path` is half a natural key, i.e. a persistence
    detail. There is no rendered `src` either: a URL built here would fix a
    width at serialisation time, and the width is the client's to choose
    through `GET /images/{id}?w=` (ADR-0032's ladder clamps it).

    `is_primary` **is** this list's order, already spent by the time a client
    sees it. Handing it over invites a client-side re-sort, which is
    `CreditResponse`'s argument about `billing_order` arriving one key over --
    and here the re-sort has a second failure: `is_primary` is a judgement
    *this project's derivation* makes (TMDb publishes no primary bit), so a
    client re-deciding on it would be re-deciding on a flag it has no way to
    interpret.

    `width`, `height` and `language` are absent for the weaker reason, and it
    is genuinely weaker for one of the three. Stored dimensions are the
    provider's originals and the proxy answers at a *rung*, so they are not
    the size of the bytes a client will get; `kind` carries the aspect-ratio
    convention a layout needs for the two kinds that have one. A logo is the
    kind where that breaks down -- logo aspect ratios really do vary -- and if
    a client needs it, adding the pair is additive where removing it would
    not be. `language` groups a wall of localised posters and no M9 surface
    paints one.

    **`kind` is not optional decoration.** It is the difference between a 2:3
    slot and a 16:9 one, and a response that dropped it would have every
    client render a backdrop as a poster with nothing reporting an error.
    It reaches `/openapi.json` as an enum, so a generated client gets the
    vocabulary rather than a string.
    """

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
    # And a *separate, independent* field (ADR-0008): the wire contract does
    # not carry a `failed` tier, because a skeleton whose enrichment failed is
    # still a perfectly usable skeleton. It is also how a *parked* enrichment
    # reaches the client -- PRD 08 forbids un-parking it behind a human's
    # back, so the honest answer is to say what happened.
    enrichment_error: str | None
    availability: list[AvailabilityResponse]
    watch_state: WatchStateResponse | None
    # **Absent when empty, and never `[]` or `null`.** The mechanism is the
    # route's `response_model_exclude_unset=True` plus an `of` that does not
    # *set* an empty one -- so the default here is the empty tuple rather than
    # `None`, and the declared type admits no null. A client's generated model
    # therefore learns "an array, or nothing", which is the whole set of
    # bodies this route can produce.
    #
    # Two other spellings were considered and one was measured. Route-level
    # `response_model_exclude_none` would take `year`, `overview`,
    # `watch_state` and `hdr_format` with it, all of which are legitimately
    # null. A `@model_serializer(mode="wrap")` that pops the key is the
    # natural pydantic idiom and **destroys the serialization JSON schema**:
    # pydantic derives it from the serializer's return annotation, so a
    # `-> dict[str, Any]` renders the whole response as `{"type": "object",
    # "additionalProperties": true}` in `/openapi.json`. Confirmed directly;
    # `test_the_absent_keys_are_still_described_in_the_schema_and_never_as_null`
    # is what stops the next reader re-deriving it.
    #
    # The cost of `exclude_unset` is that it is a rule about *every* field, so
    # a field added here and forgotten in `of` would vanish from the wire in
    # silence. `test_the_response_carries_every_field_of_its_own_model` is the
    # guard, and it is derived from `model_fields` so it grows with the model.
    cast: tuple[CreditResponse, ...] = ()
    crew: tuple[CreditResponse, ...] = ()
    # Same mechanism, same default, same declared type, for the reason above:
    # a title with no artwork -- or none this proxy can serve -- carries no
    # `images` key. The `[]` spelling is what an earlier draft of C7 shipped
    # and it is wrong; PRD 07's convention is absence, and it does not stop
    # applying on the day the table lands.
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
