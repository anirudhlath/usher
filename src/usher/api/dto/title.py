"""`GET /titles/{id}` (PRD 07).

**Four fields PRD 07's example carries are absent**, and each is deferred to
the milestone that fills it rather than shipped empty: `images` (M9's proxy),
`credits` (M7 derives `Person`/`Credit` from `raw_payloads` with no second
network call), `similar` (M6's neighbours, and its own route), and the
season/episode hierarchy (M9's `GET /series/{id}/seasons`). PRD 09's boundary
call 2 assigns all four. An empty list would be worse than an absent field: a
client cannot tell "not derived yet" from "this film has no cast", which is
the response-shaped version of the empty-dashboard-panel problem.

⚠️ **That paragraph is one field out of date and is deliberately not being
edited here.** M9 landed `credits`, as the two keys `cast` and `crew` below,
so the clause naming it is no longer true. The paragraph is rewritten **once
and whole**, by whichever of the two remaining tasks lands last -- the
`GET /series/{id}/seasons` hierarchy and the `images` key -- rather than
partially by each, which is the milestone rule for it. What survives the
rewrite is the *argument*: absence still means "nothing to say", and for
`cast`/`crew` it now means "this title has no derived credits" rather than
"this milestone has not built it". A title can carry `titles.credit_names`
and no `Credit` rows at all -- the IMDb principals loader fills that column
for ~93.8% of the catalog without deriving a single credit -- so the
underived case is the ordinary one, not the corner, and it stays
indistinguishable on the wire from a genuinely uncredited film. Recorded in
PRD 07, not closed: closing it is a per-title derived-at column, a migration
and a writer, and a fabricated `credits_derived` flag that nothing sets would
be worse than the honest silence.

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

    @classmethod
    def of(cls, detail: TitleDetail) -> "TitleResponse":
        # Set only when there is something to say. `cls(cast=(), ...)` and
        # omitting the argument build equal objects and *different* responses,
        # which is the one thing about this class worth reading twice.
        credits: dict[str, tuple[CreditResponse, ...]] = {}
        if detail.cast:
            credits["cast"] = tuple(CreditResponse.of(one) for one in detail.cast)
        if detail.crew:
            credits["crew"] = tuple(CreditResponse.of(one) for one in detail.crew)
        return cls(
            **credits,
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
