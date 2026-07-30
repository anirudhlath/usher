"""Sources and availability — the only place a media server is represented."""

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.enums import HdrFormat, SourceKind
from usher.domain.ids import new_id


class Source(DomainModel):
    """A configured backend that holds playable media."""

    id: uuid.UUID = Field(default_factory=new_id)
    kind: SourceKind
    name: str = Field(min_length=1)
    base_url: str
    credentials_ref: str  # indirection; never the secret itself
    device_id: str  # stable; registers us as a durable client
    enabled: bool = True
    supports_push: bool = False
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class MediaItem(DomainModel):
    """'This title is available on that source', plus the quality facts of
    that particular copy. A Title may have many, across sources.

    Deliberately permissive about `title_id`: NULL means unmatched, sitting
    in a review queue for manual resolution — a legitimate, expected, and
    common state, never dropped. Contrast `WatchState`, which enforces the
    opposite rule (exactly one of `title_id`/`episode_id` must be set)
    because an unattached watch record has nothing sensible to mean; an
    unmatched MediaItem has an obvious, useful one.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    title_id: uuid.UUID | None = None  # NULL => unmatched, in review queue
    episode_id: uuid.UUID | None = None
    external_id: str

    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    hdr_format: HdrFormat | None = None
    audio_channels: int | None = Field(default=None, ge=0)
    file_size_bytes: int | None = Field(default=None, ge=0)
    runtime_seconds: int | None = Field(default=None, ge=0)

    added_at: AwareDatetime | None = None
    # A MediaItem only exists because it was just observed on a source, so
    # "seen, but we don't know when" isn't a reachable state -- required,
    # matching the nullable=False last_seen_at column (Task 8). Contrast
    # added_at, which stays optional on both sides.
    last_seen_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    available: bool = True
