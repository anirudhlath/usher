"""The canonical production: one film, or one series."""

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Final

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.ids import new_id


class Title(DomainModel):
    """A canonical production.

    Identity is Usher's own UUIDv7. Provider identifiers are nullable,
    indexed *attributes* — never identity. See ADR-0003.

    Unhashable by design: `field_provenance` is a `dict[str, str]`, which
    poisons pydantic's generated `__hash__` even under `frozen=True`.
    `Source`, `MediaItem`, `User`, and `WatchState` carry no dict or list
    field and are hashable; `Title` is the one exception in this set. See
    `DomainModel`'s docstring.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    kind: TitleKind

    tmdb_id: int | None = None
    imdb_id: str | None = Field(default=None, pattern=r"^tt\d{7,8}$")
    tvdb_id: int | None = None

    name: str = Field(min_length=1)
    original_name: str | None = None
    # No normalization contract yet — stored exactly as given (articles
    # kept, casing preserved as passed). Group D puts a btree index on this
    # column for catalog ordering; if article-stripping or casefolding
    # turns out to be wanted, it belongs here as an explicit validator, not
    # as an adapter-side convention some adapters will forget.
    sort_name: str = Field(min_length=1)
    year: int | None = Field(default=None, ge=0)
    release_date: date | None = None
    end_year: int | None = Field(default=None, ge=0)  # series

    overview: str | None = None
    tagline: str | None = None
    runtime_minutes: int | None = Field(default=None, ge=0)
    status: ProductionStatus | None = None

    genres: tuple[str, ...] = Field(default_factory=tuple)
    keywords: tuple[str, ...] = Field(default_factory=tuple)
    original_language: str | None = None  # ISO 639-1, e.g. "en"
    spoken_languages: tuple[str, ...] = Field(default_factory=tuple)  # ISO 639-1
    origin_countries: tuple[str, ...] = Field(default_factory=tuple)  # ISO 3166-1 alpha-2
    content_rating: str | None = None

    # **Five fields where there were three**, because `community_rating`,
    # `vote_count` and `popularity` each had two writers meaning different
    # things: IMDb's `averageRating`/`numVotes` from `adapters/bulk/imdb.py`
    # and TMDb's `vote_average`/`vote_count` from `adapters/tmdb/mapping.py`,
    # counted over different electorates -- ~38x apart over one identified
    # population counted both ways (the frozen tier's 130,647 enriched rows:
    # median TMDb 15 against median frozen IMDb `numVotes` 576, S3). And the
    # ranges *overlap* among movies (40,518 against 40,695 on the deployed
    # catalog), so no reader could ever have told them apart by magnitude --
    # which is the load-bearing half, whatever the typical ratio. Each column
    # now names its source. ADR-0040.
    #
    # **On the two `le=10` fields the ceiling is what refuses `inf` and `NaN`,
    # not the `ge=0`**, and that is worth stating because it is an accident
    # rather than a design: `float("inf") >= 0` is `True`, so a ceiling is the
    # only thing standing between those fields and the value `tmdb_popularity`
    # accepted below until M10's F9. Pinned by `test_domain_title.py::
    # test_a_rating_refuses_a_non_finite_value_by_its_ceiling`, over both
    # fields, so a later relaxation of either ceiling cannot quietly re-open it.
    tmdb_vote_average: float | None = Field(default=None, ge=0, le=10)  # TMDb's 0-10 scale
    tmdb_vote_count: int | None = Field(default=None, ge=0)
    # **`allow_inf_nan=False`, and it is the whole of PRD 09's carried
    # "`Title.popularity` accepts infinity" debt (M10's F9).** `ge=0` alone
    # does not refuse `+inf` -- `float("inf") >= 0` is `True` -- and
    # `titles.tmdb_popularity` is `sa.Float()`, i.e. `double precision`, for
    # which IEEE `Infinity` is a perfectly legal value that also satisfies
    # `ck_titles_tmdb_popularity_non_negative`. So nothing between a TMDb
    # payload and the stored row refused it: `json.loads('1e400')` is `inf`,
    # and it sorted above every real title in every `tmdb_popularity DESC`
    # read forever.
    #
    # **ADR-0040's rename carried the defect across rather than fixing it** --
    # `popularity` became `tmdb_popularity` with the same `ge=0` and no
    # ceiling, which is why F9 still had work to do after that record landed.
    # The rename is why this comment names the new column: the debt PRD 09
    # recorded against `Title.popularity` is discharged here.
    #
    # **The bound is on the model rather than on the column, and that is
    # ADR-0043's own division of labour inverted for a reason it states.**
    # That record's rule is "the column stays the authority and the repository
    # stays the translator" -- for a column *narrower* than the field feeding
    # it. This is the opposite defect: an *unbounded* column accepting a
    # nonsense value, where there is no width to widen and no refusal to
    # translate, so the only layer that can say no is this one.
    #
    # `usher.adapters.tmdb.mapping._non_negative_float` filters non-finite
    # values to `None` **in the same commit**, because that module's contract
    # is that nothing TMDb can put in a payload may raise -- a
    # `pydantic.ValidationError` is not a `UsherPortError`, so an unfiltered
    # `inf` would escape `EnrichService`'s `except` and kill the worker
    # instead of parking the job.
    tmdb_popularity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    imdb_average_rating: float | None = Field(default=None, ge=0, le=10)  # IMDb's 0-10 scale
    imdb_num_votes: int | None = Field(default=None, ge=0)

    collection_id: uuid.UUID | None = None

    enrichment_state: EnrichmentState = EnrichmentState.SKELETON
    # Non-null means the *last* enrichment attempt failed. enrichment_state
    # is left exactly as it was — failure does not consume a tier. ADR-0008.
    enrichment_error: str | None = None
    enriched_at: AwareDatetime | None = None
    # field -> provider that supplied it
    field_provenance: dict[str, str] = Field(default_factory=dict)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


#: Every `Title` attribute whose **wire** name is not its own, and what that
#: wire name is.
#:
#: **This exists because ADR-0040's rename escaped through `GET /events`, which
#: is the one place a field name is data rather than a key.** The rename froze
#: every HTTP DTO field name deliberately -- `usher-web` is deployed against
#: them -- but `title.updated` publishes `data={"fields": [...]}` built from
#: domain attribute names, and PRD 07 defines that payload as *"Title id +
#: changed fields | Patch in place"*. So the first commit of the rename sent
#: clients `tmdb_vote_average`, `tmdb_vote_count` and `tmdb_popularity`:
#: three names that appear in **no** response body those clients can refetch,
#: and a client patching in place by them fails silently. Measured across the
#: two commits before this mapping existed.
#:
#: It lives in `domain/` and not in `api/dto/` because `services/enrich.py` is
#: what publishes the event, and the `hexagonal layering` contract orders
#: `usher.api > usher.services`, so a service importing the DTO layer is
#: `lint-imports` BROKEN rather than a style question -- verified by planting
#: exactly that import (`usher.services.enrich -> usher.api.dto.title`), inside
#: isort's position so it could not die on ruff instead. A field's published
#: name is a fact *about* the domain model anyway -- the API layer serialises
#: it, it does not decide it.
#:
#: A `MappingProxyType` for `_ORDERS`' reason: this is the constant that
#: decides what a deployed client is told changed, and one line mutating it at
#: import time would be silent and process-wide.
WIRE_FIELD_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "tmdb_vote_average": "community_rating",
        "tmdb_vote_count": "vote_count",
        "tmdb_popularity": "popularity",
    }
)


def wire_field_name(field: str) -> str:
    """`field`'s name on the wire, which is its own unless ADR-0040 moved it.

    Total rather than a lookup that can raise: every other `Title` attribute
    is published under its own name, and a `KeyError` here would turn a field
    nobody renamed into a failed enrichment.
    """
    return WIRE_FIELD_NAMES.get(field, field)
