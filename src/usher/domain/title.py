"""The canonical production: one film, or one series."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.ids import new_id


class Title(BaseModel):
    """A canonical production.

    Identity is Usher's own UUIDv7. Provider identifiers are nullable,
    indexed *attributes* — never identity. See ADR-0003.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    kind: TitleKind

    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None

    name: str
    original_name: str | None = None
    sort_name: str
    year: int | None = None
    release_date: date | None = None
    end_year: int | None = None

    overview: str | None = None
    tagline: str | None = None
    runtime_minutes: int | None = None
    status: ProductionStatus | None = None

    genres: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    original_language: str | None = None
    spoken_languages: list[str] = Field(default_factory=list)
    origin_countries: list[str] = Field(default_factory=list)
    content_rating: str | None = None

    community_rating: float | None = None
    vote_count: int | None = None
    popularity: float | None = None

    collection_id: uuid.UUID | None = None

    enrichment_state: EnrichmentState = EnrichmentState.SKELETON
    enriched_at: datetime | None = None
    field_provenance: dict[str, str] = Field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None
