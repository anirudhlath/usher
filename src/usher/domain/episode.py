"""The hierarchy under a series `Title`.

Neither model is a `Title` and neither carries a `TitleKind`: PRD 02 keeps
`TitleKind` at `movie | series` and hangs seasons and episodes off the
series. That is what makes episode watch state behave like title watch
state -- it attaches to canonical state rather than to a `MediaItem`, so it
survives the series becoming available on a second source or the first
source going away.

`Season.season_number` and `Episode.season_number` are both stored, and the
duplication is deliberate: an episode is looked up by
`(title_id, season_number, episode_number)` during ingest, before its
`Season` row is necessarily known, and a join to discover the number a
source already told us would be one query per episode across 999,827 of
them.

**Standing constraint, the same one `title.py` carries:** each model's
field set and its row's column set stay in exact 1:1 correspondence by
name. `tests/unit/test_db_models_ingest.py` checks it for free; without it
a mismatch only surfaces at read time, inside the Docker-requiring
integration suite, as an opaque `ValidationError` from `extra="forbid"`.
"""

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
