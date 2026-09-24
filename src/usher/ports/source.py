"""Port for media sources, and the DTOs that cross that boundary."""

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import UsherPortError

# The provider-id keys every adapter must emit under these exact names whenever it knows
# them.
CANONICAL_PROVIDER_IDS: frozenset[str] = frozenset({"tmdb", "imdb", "tvdb"})


class SourceEventKind(StrEnum):
    ITEM_ADDED = "item_added"
    ITEM_UPDATED = "item_updated"
    ITEM_REMOVED = "item_removed"
    WATCH_STATE_CHANGED = "watch_state_changed"


class SourceItemKind(StrEnum):
    """A source's own idea of what kind of thing an item is.

    Narrower than `usher.domain.enums.TitleKind`: sources address individual
    episodes directly, unlike `Title`.
    """

    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"


class StreamTargetKind(StrEnum):
    """What a client is expected to do with a `StreamTarget.url`.

    These values go on the wire (PRD 07), so they are an enum rather than a
    bare `str`: `"deeplink"` serialized to a client matching `"deep_link"`
    renders nothing and raises nothing.
    """

    DIRECT = "direct"
    DEEP_LINK = "deep_link"


@dataclass(frozen=True)
class SourceItem:
    """One playable item as the source describes it, already normalised.

    Nothing here is validated at construction: the annotations state a
    contract the adapter must uphold, and a naive `datetime` or a raw source
    HDR string (Emby's `"DolbyVision"`) raises only later, if anything
    re-validates it at all.

    `provider_ids` keys are lowercase, using `CANONICAL_PROVIDER_IDS`' names
    where they apply.
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
    # Opaque, and never stored -- PRD 03 caches provider responses only --
    # and never interpreted above the adapter boundary. Every typed field above
    # exists so that nothing above the adapter has to read this one.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceWatchState:
    """One item's watch state as a source reports it."""

    external_id: str
    position_seconds: int
    played: bool
    play_count: int | None = None
    last_played_at: AwareDatetime | None = None
    # `None` means the source didn't distinguish, which lands everything on
    # the singleton default user. Carried now because adding it once a
    # household has two users is a breaking DTO change.
    source_user_id: str | None = None


@dataclass(frozen=True)
class WatchStateUpdate:
    position_seconds: int
    played: bool


@dataclass(frozen=True)
class SourceEvent:
    """One thing a source's push channel said changed."""

    kind: SourceEventKind
    external_ids: tuple[str, ...] = field(default_factory=tuple)
    watch_states: tuple[SourceWatchState, ...] = ()

    def __post_init__(self) -> None:
        named = set(self.external_ids)
        unnamed = [
            state.external_id for state in self.watch_states if state.external_id not in named
        ]
        if unnamed:
            raise ValueError(
                "a carried watch state must name an item the event listed in external_ids; "
                f"unlisted: {sorted(unnamed)}"
            )


def redact_query(url: str) -> str:
    """A URL cut at its query string, for rendering in a `repr` or a log line."""
    cut = min((index for index in (url.find("?"), url.find("#")) if index != -1), default=-1)
    return url if cut < 0 else f"{url[:cut]}<redacted>"


# The scheme a deep-link target opens the client with. One name, here, so no
# adapter grows a second spelling of it.
INFUSE_SCHEME = "infuse"


def wrap_deep_link(inner_url: str) -> str:
    """Wrap a URL as an Infuse `x-callback-url` deep link."""
    return f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(inner_url, safe='')}"


# `repr=False` is load-bearing, not stylistic -- see `__repr__` below.
@dataclass(frozen=True, repr=False)
class StreamTarget:
    """How to play an item.

    Clients choose between the returned targets.
    """

    kind: StreamTargetKind
    url: str
    scheme: str | None = None
    container: str | None = None
    video_codec: str | None = None
    audio: str | None = None
    hdr_format: HdrFormat | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None

    def __repr__(self) -> str:
        """The generated `repr` with `url` redacted.

        `url` carries a source credential in its query string, and a `repr`
        of this reaches logs and tracebacks. Both halves fail safe: with
        `repr=False`, deleting this method falls back to `object.__repr__`,
        which leaks nothing, and `dataclasses` never overwrites a `__repr__`
        defined in the class body, so restoring `repr=True` does not restore
        the leaking one. Only removing both re-opens it.

        The cut is `redact_query`, not inlined: the push channel's socket URL
        carries the same token in the same shape.
        """
        rendered = {item.name: getattr(self, item.name) for item in fields(self)}
        rendered["url"] = redact_query(self.url)
        body = ", ".join(f"{name}={value!r}" for name, value in rendered.items())
        return f"{type(self).__name__}({body})"


@dataclass(frozen=True)
class SourceStatus:
    """What `GET /admin/sources/{id}/status` (PRD 07) needs to report.

    Independent booleans rather than one enum: "reachable, credentials
    wrong" and "reachable, authenticated, proxy stripping `Upgrade`" are
    both real states.

    `push_available` defaults to `None`, meaning not probed. A WebSocket
    handshake against a nonexistent path also upgrades, so an upgrade is not
    evidence; only received messages are. An adapter that has not asserted on
    messages reports `None` and the admin surface renders "unknown".

    `detail` is a short operator-facing status line and must never carry a
    credential -- build it from translated `UsherPortError`s, whose messages
    carry a method, a path, and a transport error.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None = None
    server_version: str | None = None
    # `None` means "not determined", as `push_available` does. An operator
    # configuring an administrator account is an accepted risk mitigated by
    # guidance, not by code, so nothing here refuses it.
    is_administrator: bool | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        # No clause for `is_administrator`: the two below refuse states no
        # upstream produces, but an administrator account is a state real
        # deployments are in, and the screen reporting it must be
        # constructible.
        if self.authenticated and not self.reachable:
            raise ValueError("a source cannot be authenticated without being reachable")
        if self.push_available and not self.authenticated:
            raise ValueError("push cannot be available without being authenticated")


@dataclass(frozen=True)
class PushProbe:
    """What an on-demand push probe learned."""

    upgraded: bool
    delivering: bool
    events: tuple[SourceEventKind, ...] = ()
    detail: str | None = None


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
        """Whether this adapter has a live push channel right now.

        Grounded in messages received, never in a socket being open.
        """

    @abstractmethod
    async def verify(self) -> "SourceStatus":
        """Report reachability, authentication, and push availability.

        Returns rather than raises for every expected failure -- unreachable
        host, rejected credentials, rate-limited upstream -- because its
        caller renders those states rather than handling them. The one
        exception to the `usher.ports.errors` taxonomy, which still governs
        every other method here.

        Must not claim `push_available=True` without message-level evidence.
        """

    @abstractmethod
    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, or only items changed since a cursor."""

    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None:
        """Fetch one item.

        `None` means gone from the source, and reconcile marks it
        `available = false`. A transient failure to reach the source must
        raise (`PortUnavailable`), never return `None` -- conflating the two
        marks healthy items unavailable on a flaky network.
        """

    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        """Ranked ways to play an item, best first.

        Empty, not an error, for anything unplayable -- a series or season
        folder, or an unknown id. The caller's next move is the same either
        way, and `get_item` already tells absence from presence.
        """

    @abstractmethod
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor."""

    @abstractmethod
    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        """Authoritative watch state for one item, including play history."""

    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to the source.

        Must raise on failure, never swallow it. "Best-effort" describes the
        caller, which enqueues a retry rather than failing the triggering
        request; a swallowed error here means that retry never happens.
        """

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel.

        Adapters without one raise `SourceNotSupported`; the reconciler
        covers them. Offering a channel is not a claim it is delivering --
        see `supports_push`.

        One connection per call, never a cached one: a supervisor calls this
        once per reconnect, and a cached channel hands back a closed socket
        forever.

        Must raise, never truncate. An iterator that stops because the
        connection died reads to a supervisor as a clean shutdown, and it
        never reconnects.
        """

    @property
    def push_reconnects(self) -> int:
        """How many times this adapter's push channel has reopened."""
        return 0

    async def probe_push(self, *, timeout_seconds: float = 15.0) -> PushProbe:
        """Open the push channel, wait, and report what arrived."""
        collected: list[SourceEventKind] = []
        upgraded = False
        try:
            async with self.events() as events:
                # Set inside the block so a failed upgrade reports
                # `upgraded=False` while a channel that opened and then went
                # stale reports `True`. The operator's next move differs.
                upgraded = True
                stream = aiter(events)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_seconds
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(anext(stream), timeout=remaining)
                    except (TimeoutError, StopAsyncIteration):
                        break
                    collected.append(event.kind)
                return PushProbe(
                    upgraded=True,
                    # Read from the adapter, never from `collected`: an idle
                    # library's channel delivers messages that map to no
                    # event, and those still prove it alive.
                    delivering=self.supports_push,
                    events=tuple(dict.fromkeys(collected)),
                )
        except UsherPortError as exc:
            # `False`, not `self.supports_push`: the context manager has
            # already exited here, so the ledger reports closed anyway --
            # spelled as a constant so nobody has to work that out.
            return PushProbe(
                upgraded=upgraded,
                delivering=False,
                events=tuple(dict.fromkeys(collected)),
                detail=str(exc),
            )

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources -- connection pools, the push WebSocket.

        Idempotent: a shutdown path and a delete path can both reach it.
        Afterwards every other method raises `PortUnavailable`, not whatever
        the underlying client raises -- a closed `httpx.AsyncClient` raises a
        bare `RuntimeError`, which is no `httpx.HTTPError` and escapes an
        adapter translating only those.
        """


class SourceAdapterFactory(ABC):
    """Builds the right `SourceAdapter` for a configured `Source`.

    `services/` may depend only on `domain/` and `ports/`, so `SourceService`
    cannot import an adapter -- it receives one. The single registration
    point for a source kind: a new adapter adds a `SourceKind` member and one
    branch here, and nothing else moves.
    """

    @abstractmethod
    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        """Construct an adapter.

        The caller owns it and must `aclose()` it.

        Raises `SourceNotSupported` for a `Source.kind` this factory has no
        implementation for.
        """
