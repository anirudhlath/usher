"""The canonical production: one film, or one series."""

import uuid
from datetime import UTC, date, datetime

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

    # **The `le=10` is what refuses `inf` and `NaN` here, not the `ge=0`**, and
    # that is worth stating because it is an accident rather than a design:
    # `float("inf") >= 0` is `True`, so a ceiling is the only thing standing
    # between this field and the value `popularity` used to accept. Pinned by
    # `test_domain_title.py::
    # test_community_rating_refuses_a_non_finite_value_by_its_ceiling`, so a
    # later relaxation of the ceiling cannot quietly re-open it.
    community_rating: float | None = Field(default=None, ge=0, le=10)  # TMDb's 0-10 scale
    vote_count: int | None = Field(default=None, ge=0)
    # **`allow_inf_nan=False`, and it is the whole of PRD 09's carried
    # "`Title.popularity` accepts infinity" debt (M10's F9).** `ge=0` alone
    # does not refuse `+inf` -- `float("inf") >= 0` is `True` -- and
    # `titles.popularity` is `sa.Float()`, i.e. `double precision`, for which
    # IEEE `Infinity` is a perfectly legal value that also satisfies
    # `ck_titles_popularity_non_negative`. So nothing between a TMDb payload
    # and the stored row refused it: `json.loads('1e400')` is `inf`, and it
    # sorted above every real title in every `popularity DESC` read forever.
    #
    # **The bound is on the model rather than on the column, and that is
    # ADR-0041's own division of labour inverted for a reason it states.**
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
    popularity: float | None = Field(default=None, ge=0, allow_inf_nan=False)

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
