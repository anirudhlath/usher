"""Sources and availability — the only place a media server is represented."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id


class Source(BaseModel):
    """A configured backend that holds playable media."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    kind: SourceKind
    name: str
    base_url: str
    credentials_ref: str
    device_id: str
    enabled: bool = True
    supports_push: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MediaItem(BaseModel):
    """'This title is available on that source', plus the quality facts of
    that particular copy. A Title may have many, across sources."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    external_id: str

    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    hdr_format: str | None = None
    audio_channels: int | None = None
    file_size_bytes: int | None = None
    runtime_seconds: int | None = None

    added_at: datetime | None = None
    last_seen_at: datetime | None = None
    available: bool = True
