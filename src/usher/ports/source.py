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
    """A source's own idea of what kind of thing an item is — narrower
    than `usher.domain.enums.TitleKind` because sources address individual
    episodes directly, unlike `Title`."""

    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"


class StreamTargetKind(StrEnum):
    """What a client is expected to do with a `StreamTarget.url`.

    A `StrEnum` rather than the bare `str` this field carried through M1 and
    M2, for the reason `SourceItemKind` exists: PRD 07 puts these values on
    the wire, and a bare `str` invites `"deeplink"` (no underscore) to be
    serialized to a client that matches on `"deep_link"` and silently
    renders nothing.
    """

    DIRECT = "direct"
    DEEP_LINK = "deep_link"


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

    `provider_ids` keys are lowercase and use `CANONICAL_PROVIDER_IDS`'
    names where they apply — see that constant.
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
    """One item's watch state as a source reports it."""

    external_id: str
    position_seconds: int
    played: bool
    play_count: int | None = None
    last_played_at: AwareDatetime | None = None
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


# The scheme a deep-link target opens the client with. Kept under this exact
# name across the move D2 made -- see `wrap_deep_link` below -- because a
# second spelling of one constant is exactly what the move exists to
# prevent, and an earlier draft of this task's own plan used two names for
# it in two paragraphs.
INFUSE_SCHEME = "infuse"


def wrap_deep_link(inner_url: str) -> str:
    """Wrap a URL as an Infuse `x-callback-url` deep link."""
    return f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(inner_url, safe='')}"


# `repr=False` is load-bearing, not stylistic -- see `__repr__` below.
@dataclass(frozen=True, repr=False)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets."""

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
        """The generated `repr` with `url` redacted — see the class
        docstring for why this is a security property rather than taste.

        Both halves fail safe. `@dataclass(repr=False)` means deleting this
        method yields `object.__repr__` (`<StreamTarget object at 0x…>`),
        which leaks nothing; and `dataclasses` never overwrites a
        `__repr__` already defined in the class body, so flipping
        `repr=False` back to `repr=True` does not silently restore the
        leaking one either. Only deleting *both* re-opens it, which is what
        `tests/unit/test_ports_source.py` is there to catch.

        The cut itself is `redact_query` above and is deliberately *not*
        inlined here: from M5 the push channel's socket URL carries the same
        token in the same shape, and two copies of one rule is how the two
        come to disagree.
        """
        rendered = {item.name: getattr(self, item.name) for item in fields(self)}
        rendered["url"] = redact_query(self.url)
        body = ", ".join(f"{name}={value!r}" for name, value in rendered.items())
        return f"{type(self).__name__}({body})"


@dataclass(frozen=True)
class SourceStatus:
    """What `GET /admin/sources/{id}/status` (PRD 07) needs to report.

    Three booleans rather than one enum, because the states are
    independent: "reachable but the credentials are wrong" and "reachable,
    authenticated, but a proxy is stripping `Upgrade`" are both real, and a
    flat enum would have to enumerate the product.

    `push_available` is `bool | None`, and `None` — "not probed" — is the
    default. This is ADR-0004's health-check caveat in DTO form: a
    WebSocket handshake against a *nonexistent* path also upgrades and also
    receives `Sessions`, so a successful upgrade is not evidence of
    anything. Only *received messages* are. Until M5 builds a probe that
    asserts on messages, every adapter reports `None` here, and the admin
    surface renders "unknown" rather than a guess.

    `detail` is a short operator-facing string — a status line, not a
    payload. It must never carry a credential: an implementation builds it
    from its own translated `UsherPortError`s, whose messages carry a
    method, a path, and a transport error, never a token or a password.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None = None
    server_version: str | None = None
    # `None` means "not determined", exactly as `push_available` does, and for a
    # stronger reason: ADR-0012 accepts the risk that an operator configures an
    # administrator account, and its recorded mitigation is guidance rather than code.
    is_administrator: bool | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        # Deliberately no clause for `is_administrator`. The two above refuse
        # states no upstream produces; an administrator account is a state a
        # real deployment is in right now, and the screen that exists to
        # report it must be able to construct a status for it.
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
        """Whether this adapter has a live push channel right now, **and the answer must be
        grounded in messages received rather than in a socket being open.**
        """

    @abstractmethod
    async def verify(self) -> "SourceStatus":
        """Report reachability, authentication, and push availability.

        Returns rather than raises for every *expected* failure —
        unreachable host, rejected credentials, a rate-limited upstream —
        because its one caller (`GET /admin/sources/{id}/status`, PRD 07)
        exists to render those states, not to handle them. The taxonomy in
        `usher.ports.errors` still governs every other method on this port;
        this is the deliberate exception, and it is why the method returns
        a `SourceStatus` rather than a bool.

        Must not claim `push_available=True` without message-level
        evidence — see `SourceStatus`.
        """

    @abstractmethod
    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, or only items changed since a cursor."""

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
        """Ranked ways to play an item, best first.

        Empty for an item there is no way to play — a series or season
        folder, or an id the source does not have. Not an error: the
        caller's next move is identical in both cases ("not playable
        here"), and `get_item` already exists to tell absence from
        presence, so raising would only make the common case
        (`POST /titles/{id}/play` for something owned but not playable)
        travel through an exception path.
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

        Must raise on failure, never swallow it. PRD 03's "best-effort"
        describes the *caller's* behaviour — the request that triggered
        this write never blocks or fails on a write-back error, because
        the caller enqueues a retry instead — not this method's. That
        guarantee only works if failures are visible: an implementation
        that swallows an error here means the retry never happens.
        """

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise `SourceNotSupported`;
        the reconciler covers them. See `supports_push` for the one-way
        relationship between the two — offering a channel is not a claim
        that it is delivering.

        One connection per call, not a cached one: a supervisor calls this
        once per reconnect, and a cached channel hands back a closed socket
        forever.

        Same must-raise-never-truncate rule as `list_items`: an iterator
        that *stops* because the connection died is indistinguishable from a
        source with nothing more to say, and a supervisor would read that as
        a clean shutdown and never reconnect. A channel that has stopped
        delivering raises rather than sitting there looking well.
        """

    @property
    def push_reconnects(self) -> int:
        """How many times this adapter's push channel has re-**opened**."""
        return 0

    async def probe_push(self, *, timeout_seconds: float = 15.0) -> PushProbe:
        """Open the push channel, wait, and report **what arrived**."""
        collected: list[SourceEventKind] = []
        upgraded = False
        try:
            async with self.events() as events:
                # Set *inside* the block: a failed upgrade must report
                # `upgraded=False`, and a channel that opened and then went
                # stale must not — the second is the failure ADR-0004
                # warns about and the operator's next move differs.
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
                    # Read from the adapter, never from `collected`: an
                    # idle library's channel delivers messages that map to
                    # no event at all, and that is precisely what keeps it
                    # measurably alive.
                    delivering=self.supports_push,
                    events=tuple(dict.fromkeys(collected)),
                )
        except UsherPortError as exc:
            # `False`, not `self.supports_push`: the channel's context
            # manager has already exited by the time this runs, so the
            # ledger reports closed anyway — spelled as the constant so a
            # reader does not have to reason about that to trust it.
            return PushProbe(
                upgraded=upgraded,
                delivering=False,
                events=tuple(dict.fromkeys(collected)),
                detail=str(exc),
            )

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — connection pools, and (from M5) the
        push WebSocket. Called when a source is deleted (`DELETE
        /admin/sources/{id}`, PRD 07) or the process shuts down.

        Idempotent: calling it twice is not an error, because a shutdown
        path and a delete path can both reach it. Afterwards every other
        method raises `PortUnavailable` rather than whatever the underlying
        client happens to raise — verified: a closed `httpx.AsyncClient`
        raises a bare `RuntimeError`, which is not an `httpx.HTTPError` and
        so escapes an adapter that only translates those.
        """


class SourceAdapterFactory(ABC):
    """Builds the right `SourceAdapter` for a configured `Source`.

    Exists because `services/` may depend only on `domain/` and `ports/`
    (PRD 01, layering rule 2), so `SourceService` cannot import
    `EmbyAdapter` — it receives one. This is also the single place a second
    source kind gets registered, which is the concrete form of PRD 01's
    "additional sources" extension seam: a Jellyfin adapter adds a
    `SourceKind` member and one branch here, and nothing else in the
    application moves.
    """

    @abstractmethod
    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        """Construct an adapter. The caller owns it and must `aclose()` it.

        Raises `SourceNotSupported` for a `Source.kind` this factory has no
        implementation for.
        """
