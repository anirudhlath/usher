"""Users and watch state.

Watch state attaches to the canonical Title, not to a MediaItem, so it
survives adding, changing, or losing a source.
"""

import uuid
from datetime import UTC, datetime
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from usher.domain.base import DomainModel
from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id


class User(DomainModel):
    """An Usher-owned account — not an Emby (or any other source's) user.

    Usher is not multi-tenant; `User` exists so watch state and taste are
    per-person within a household, not so Usher can be run as a shared
    service. v1 has no authentication: every request resolves to the
    singleton default user (`is_default=True`). Adding real auth later
    replaces that one lookup without moving anything else, because watch
    state and taste are already keyed by `User.id`.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    name: str = Field(min_length=1)
    is_default: bool = False
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchState(DomainModel):
    """Progress or completion of one user against one Title or Episode.

    Exactly one of `title_id`/`episode_id` must be set — enforced below.
    Contrast `MediaItem`, which is deliberately permissive about its
    equivalent `title_id` (there, NULL means "unmatched, review queue", a
    legitimate and common state). An unattached `WatchState` has no such
    reading: it would not be progress on anything.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    user_id: uuid.UUID
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None

    position_seconds: int = Field(default=0, ge=0)
    runtime_seconds: int | None = Field(default=None, ge=0)
    played: bool = False
    play_count: int = Field(default=0, ge=0)
    last_played_at: AwareDatetime | None = None

    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    # Who last wrote this record: source | api. Not a user reference (that's
    # user_id, immediately above it in most schemas — which is exactly why
    # this field is *not* called updated_by; that name reads as a user FK
    # here). No default: a sync path that forgets to set this must fail
    # loudly rather than silently mislabel source-pushed state as
    # user-originated.
    origin: WatchStateOrigin

    @model_validator(mode="after")
    def _exactly_one_of_title_or_episode(self) -> Self:
        if (self.title_id is None) == (self.episode_id is None):
            raise ValueError("exactly one of title_id or episode_id must be set")
        return self
