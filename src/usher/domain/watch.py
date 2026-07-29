"""Users and watch state.

Watch state attaches to the canonical Title, not to a MediaItem, so it
survives adding, changing, or losing a source.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    name: str
    is_default: bool = False
    created_at: datetime | None = None


class WatchState(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    user_id: uuid.UUID
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None

    position_seconds: int = 0
    runtime_seconds: int | None = None
    played: bool = False
    play_count: int = 0
    last_played_at: datetime | None = None

    updated_at: datetime | None = None
    updated_by: WatchStateOrigin = WatchStateOrigin.API
