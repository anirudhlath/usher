"""Port for media sources, and the DTOs that cross that boundary."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat
from usher.ports.errors import UsherPortError


class SourceEventKind(StrEnum):
    ITEM_ADDED = "item_added"
    ITEM_UPDATED = "item_updated"
    ITEM_REMOVED = "item_removed"
    WATCH_STATE_CHANGED = "watch_state_changed"


class SourceItemKind(StrEnum):
    """A source's own idea of what kind of thing an item is — narrower
    than `usher.domain.enums.TitleKind` because sources address individual
    episodes directly, unlike `Title`."""

    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"


@dataclass(frozen=True)
class SourceItem:
    """One playable item as the source describes it, already normalised.

    A plain dataclass, not a `DomainModel` — nothing here is validated at
    construction. `SourceItemKind`, `HdrFormat`, and `AwareDatetime` below
    state the contract an adapter must uphold, the same way `MediaItem`
    and `Title` enforce it on the far side of the ingest boundary;
    constructing this with a naive `datetime` or a source's raw HDR string
    (e.g. Emby's `"DolbyVision"`) will not raise here — only later, if and
    when something re-validates it, which is one layer too late.
    """

    external_id: str
    name: str
    kind: SourceItemKind
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    hdr_format: HdrFormat | None = None
    audio_channels: int | None = None
    file_size_bytes: int | None = None
    runtime_seconds: int | None = None
    added_at: AwareDatetime | None = None
    series_external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    # Opaque; stored in raw_payloads (PRD 03) for debugging and future
    # reprocessing, never interpreted above the adapter boundary. The one
    # deliberate exception to "nothing source-specific escapes its
    # adapter" — every other field above exists so this one doesn't have
    # to be read by anything above the adapter.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceWatchState:
    external_id: str
    position_seconds: int
    played: bool
    play_count: int = 0
    last_played_at: datetime | None = None
    # Emby is multi-user; None means "the source didn't distinguish", which
    # today is fine because everything implicitly lands on the singleton
    # default user (PRD 01's authentication seam). Cheap to carry now —
    # becomes a breaking DTO change the moment a household has two users.
    source_user_id: str | None = None


@dataclass(frozen=True)
class WatchStateUpdate:
    position_seconds: int
    played: bool


@dataclass(frozen=True)
class SourceEvent:
    """🔶 Provisional — carries no payload, so a `WATCH_STATE_CHANGED`
    event forces the push lane to re-walk `watch_state(since=...)` to
    discover what changed, even though Emby's own `UserDataChanged`
    message already carries the position and played flag. Settle in M5,
    when the push lane is actually built and the cost of re-walking is
    measurable against just carrying the payload through.
    """

    kind: SourceEventKind
    external_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets.

    🔶 Provisional — PRD 07's `/play` response example includes `scheme`
    (for `kind: "deep_link"` targets like `infuse://...`) and `audio`
    (e.g. `"truehd_atmos_7_1"`) that this shape doesn't carry yet. Settle
    in M3, alongside the Emby adapter that first has to populate this.
    """

    kind: str
    url: str
    container: str | None = None
    video_codec: str | None = None
    hdr_format: str | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None


class SourceNotSupported(UsherPortError):
    """Raised by adapters for capabilities they do not have."""


class SourceAdapter(ABC):
    """A backend that holds playable media.

    Nothing source-specific may escape an implementation of this port.
    """

    @property
    @abstractmethod
    def source_id(self) -> uuid.UUID:
        """The configured Source this adapter serves."""

    @property
    @abstractmethod
    def supports_push(self) -> bool:
        """Whether this adapter has a live push channel right now. PRD 03:
        when the socket can't be established (or drops and stays down
        after N reconnect attempts), the adapter reports this `False` and
        the reconciler's nightly walk covers the gap. Mirrors
        `usher.domain.source.Source.supports_push`, which this populates.
        """

    @abstractmethod
    async def verify(self) -> bool:
        """Authenticate and confirm reachability.

        🔶 Provisional — a single bool cannot distinguish "bad
        credentials" from "unreachable" from "reachable but a proxy
        stripped `Upgrade`", all of which `GET /admin/sources/{id}/status`
        (PRD 07) needs to report separately. The error taxonomy in
        `usher.ports.errors` is the prerequisite for settling this (raise
        `PortAuthFailed`/`PortUnavailable` instead of returning `False`?)
        — deferred to M3, when the Emby adapter and the admin status
        endpoint are built together.
        """

    @abstractmethod
    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, or only items changed since a cursor.

        Contract an implementation must guarantee:
        - `since` is inclusive: an item changed exactly at `since` is
          included, never dropped at the boundary.
        - No ordering is promised across items; callers must not rely on
          one.
        - The same item may be yielded more than once in a single walk
          (e.g. a paginated upstream listing whose pages overlap); callers
          deduplicate by `external_id`.
        - **Must raise, never truncate silently.** An iterator that stops
          because the walk finished is indistinguishable from one that
          stopped because the adapter swallowed an error — and the
          reconciler cannot tell the difference; it would mark the rest of
          the library `available = false`. A partial failure raises (e.g.
          `PortUnavailable` from `usher.ports.errors`) from the generator;
          it does not just stop yielding.
        """

    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None:
        """Fetch one item.

        `None` means the item is gone from the source — PRD 03's
        reconcile marks it `available = false`. A transient failure to
        reach the source is a different outcome and must raise (e.g.
        `PortUnavailable` from `usher.ports.errors`), never be reported as
        `None`; conflating the two would mark a healthy item unavailable
        because of a flaky network, not because it was actually deleted.
        """

    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        """Ranked ways to play an item."""

    @abstractmethod
    def watch_state(self, since: datetime | None = None) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor.

        Same `since`-inclusivity, no-ordering, possible-duplicates, and
        must-raise-never-truncate contract as `list_items`.
        """

    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to the source.

        Must raise on failure, never swallow it. PRD 03's "best-effort"
        describes the *caller's* behaviour — the request that triggered
        this write never blocks or fails on a write-back error, because
        the caller enqueues a retry instead — not this method's. That
        guarantee only works if failures are visible: an implementation
        that swallows an error here means the retry never happens.
        """

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise SourceNotSupported; the
        reconciler covers them."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — connection pools, and (from M5) the
        push WebSocket. Called when a source is deleted (`DELETE
        /admin/sources/{id}`, PRD 07) or the process shuts down."""
