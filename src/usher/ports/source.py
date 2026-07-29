"""Port for media sources, and the DTOs that cross that boundary."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SourceEventKind(StrEnum):
    ITEM_ADDED = "item_added"
    ITEM_UPDATED = "item_updated"
    ITEM_REMOVED = "item_removed"
    WATCH_STATE_CHANGED = "watch_state_changed"


@dataclass(frozen=True)
class SourceItem:
    """One playable item as the source describes it, already normalised."""

    external_id: str
    name: str
    kind: str
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
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
    series_external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceWatchState:
    external_id: str
    position_seconds: int
    played: bool
    play_count: int = 0
    last_played_at: datetime | None = None


@dataclass(frozen=True)
class WatchStateUpdate:
    position_seconds: int
    played: bool


@dataclass(frozen=True)
class SourceEvent:
    kind: SourceEventKind
    external_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets."""

    kind: str
    url: str
    container: str | None = None
    video_codec: str | None = None
    hdr_format: str | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None


class SourceNotSupported(Exception):
    """Raised by adapters for capabilities they do not have."""


class SourceAdapter(ABC):
    """A backend that holds playable media.

    Nothing source-specific may escape an implementation of this port.
    """

    @property
    @abstractmethod
    def source_id(self) -> uuid.UUID:
        """The configured Source this adapter serves."""

    @abstractmethod
    async def verify(self) -> bool:
        """Authenticate and confirm reachability."""

    @abstractmethod
    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, optionally limited to changes since a cursor."""

    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None:
        """Fetch one item, or None if it is gone."""

    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        """Ranked ways to play an item."""

    @abstractmethod
    def watch_state(self, since: datetime | None = None) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor."""

    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back. Best-effort; may raise."""

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise SourceNotSupported; the
        reconciler covers them."""
