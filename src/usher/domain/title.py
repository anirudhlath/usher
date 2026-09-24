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

    Identity is Usher's own UUIDv7. Provider identifiers are nullable, indexed
    *attributes* — never identity.

    Unhashable by design: `field_provenance` is a `dict[str, str]`, which
    poisons pydantic's generated `__hash__` even under `frozen=True`.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    kind: TitleKind

    tmdb_id: int | None = None
    imdb_id: str | None = Field(default=None, pattern=r"^tt\d{7,8}$")
    tvdb_id: int | None = None

    name: str = Field(min_length=1)
    original_name: str | None = None
    # No normalization contract — stored exactly as given, articles kept, casing
    # preserved. If article-stripping or casefolding turns out to be wanted, it
    # belongs here as an explicit validator, not as an adapter-side convention
    # some adapters will forget.
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

    # **Five fields where there were three**: IMDb's `averageRating`/`numVotes`
    # and TMDb's `vote_average`/`vote_count` count different electorates, so one
    # shared column meant whatever its last writer meant.
    tmdb_vote_average: float | None = Field(default=None, ge=0, le=10)  # TMDb's 0-10 scale
    tmdb_vote_count: int | None = Field(default=None, ge=0)
    tmdb_popularity: float | None = Field(default=None, ge=0)
    imdb_average_rating: float | None = Field(default=None, ge=0, le=10)  # IMDb's 0-10 scale
    imdb_num_votes: int | None = Field(default=None, ge=0)

    collection_id: uuid.UUID | None = None

    enrichment_state: EnrichmentState = EnrichmentState.SKELETON
    # Non-null means the *last* enrichment attempt failed. `enrichment_state` is
    # left exactly as it was — failure does not consume a tier.
    enrichment_error: str | None = None
    enriched_at: AwareDatetime | None = None
    # field -> provider that supplied it
    field_provenance: dict[str, str] = Field(default_factory=dict)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


#: Every `Title` attribute whose **wire** name is not its own, and what that
#: wire name is.
WIRE_FIELD_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "tmdb_vote_average": "community_rating",
        "tmdb_vote_count": "vote_count",
        "tmdb_popularity": "popularity",
    }
)


def wire_field_name(field: str) -> str:
    """`field`'s name on the wire, which is its own unless it was renamed.

    Total rather than a lookup that can raise: every other `Title` attribute is
    published under its own name, and a `KeyError` here would turn a field
    nobody renamed into a failed enrichment.
    """
    return WIRE_FIELD_NAMES.get(field, field)
