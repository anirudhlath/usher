"""The hierarchy under a series `Title`."""

import uuid
from datetime import UTC, date, datetime

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class Season(DomainModel):
    """One season of a series.

    `season_number` may be `0`: TMDb numbers a series' specials as season 0
    and Emby emits `ParentIndexNumber: 0` for them, so a `ge=1` bound would
    silently drop every special in the library.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    title_id: uuid.UUID
    season_number: int = Field(ge=0)

    name: str | None = None
    overview: str | None = None
    air_date: date | None = None
    episode_count: int | None = Field(default=None, ge=0)
    tmdb_id: int | None = None

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class Episode(DomainModel):
    """One episode. First-class -- it carries watch state and it is what a
    source actually holds -- but not independently searchable in v1
    (PRD 05).

    `absolute_number` is nullable and stays that way: it is TVDb's ordering
    concept, TMDb does not supply it, and an alternate-ordering provider is
    an explicitly post-v1 candidate (PRD 09).
    """

    id: uuid.UUID = Field(default_factory=new_id)
    title_id: uuid.UUID
    season_id: uuid.UUID
    season_number: int = Field(ge=0)
    episode_number: int = Field(ge=0)
    absolute_number: int | None = Field(default=None, ge=0)

    name: str | None = None
    overview: str | None = None
    air_date: date | None = None
    runtime_minutes: int | None = Field(default=None, ge=0)
    tmdb_id: int | None = None
    imdb_id: str | None = Field(default=None, pattern=r"^tt\d{7,8}$")

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
