"""Port for media sources, and the DTOs that cross that boundary."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import UsherPortError

# The provider-id keys every adapter must emit under these exact names
# whenever it knows them. Sources spell them differently -- Emby's
# `ProviderIds` uses `Tmdb`/`Imdb`/`Tvdb` -- and normalising at the adapter
# boundary is what keeps M4's matcher from having to know one casing per
# source. Keys outside this set are permitted (a source that knows an AniDB
# id should say so) but must be lowercase, so the rule is "lowercase always,
# these three names when known" rather than a closed vocabulary.
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
    external_id: str
    position_seconds: int
    played: bool
    play_count: int = 0
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
    """🔶 Provisional — carries no payload, so a `WATCH_STATE_CHANGED`
    event forces the push lane to re-walk `watch_state(since=...)` to
    discover what changed, even though Emby's own `UserDataChanged`
    message already carries the position and played flag. Settle in M5,
    when the push lane is actually built and the cost of re-walking is
    measurable against just carrying the payload through.

    Reviewed in M3 and deliberately left alone: M3 builds no push lane, so
    the measurement this marker is waiting for is still not available.
    """

    kind: SourceEventKind
    external_ids: tuple[str, ...] = field(default_factory=tuple)


def _redacted(url: str) -> str:
    """A playback URL cut at its query string, for rendering in a `repr`.

    Everything from the first `?` or `#` onward, gone. Not a search for
    `api_key=`: the deep-link target hides the whole direct URL, token and
    all, percent-encoded inside its *own* query string, so a redaction that
    matched on a parameter name would sail straight past it. Cutting at the
    query covers both, and covers whatever a second source spells its token
    parameter — which is the point, since `StreamTarget` is the port's DTO
    and Jellyfin's `ApiKey` has exactly the same problem as Emby's
    `api_key`.

    Enough is kept (scheme, host, path) to identify the item in a log line;
    the query carries no fact a reader needs that the target's own typed
    fields do not already state.
    """
    cut = min((index for index in (url.find("?"), url.find("#")) if index != -1), default=-1)
    return url if cut < 0 else f"{url[:cut]}<redacted>"


# `repr=False` is load-bearing, not stylistic -- see `__repr__` below.
@dataclass(frozen=True, repr=False)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets.

    Complete information, not a decision: PRD 07's playback contract is
    that Usher "supplies complete information and never proxies bytes",
    so a target carries everything a client needs to decide whether it can
    play this — container, codecs, HDR format, resolution — rather than a
    server-side guess at which one it should use.

    `scheme` is set only for `StreamTargetKind.DEEP_LINK` targets and names
    the URL scheme (`"infuse"` for `infuse://…`), so a client can check
    whether it can handle the link without parsing the URL. `audio` is a
    single lowercase token describing the default audio track as a client
    thinks about it (`"truehd_atmos_7_1"`), which is a different thing from
    `SourceItem.audio_codec`'s raw `"truehd"` — the codec alone does not
    tell a client whether it can play the track.

    **`url` carries a source credential, and `repr` therefore does not
    render it.** A direct-play target has to authenticate itself to the
    source — Emby's `api_key`, Jellyfin's `ApiKey` — because Usher never
    proxies the bytes (ADR-0012, `docs/prd/decisions/`).
    That makes this the one DTO on any port that deliberately holds a
    secret, and PRD 08's "credentials are never logged, including in error
    paths and request dumps" cannot then be a rule each caller remembers:
    `logger.info(targets)`, an f-string in an exception message, a pytest
    assertion dump, and loguru's `diagnose=True` frame-locals renderer all
    reach the value through `__repr__` and nothing else. So the guarantee
    lives here, once.

    Verified directly: with the generated `repr`, the token appears in
    plain text in all four. `.url` itself is untouched — PRD 07's `/play`
    response is built from it, and a scrubbed URL would be an unplayable
    link.
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
        """The generated `repr` with `url` redacted — see the class
        docstring for why this is a security property rather than taste.

        Both halves fail safe. `@dataclass(repr=False)` means deleting this
        method yields `object.__repr__` (`<StreamTarget object at 0x…>`),
        which leaks nothing; and `dataclasses` never overwrites a
        `__repr__` already defined in the class body, so flipping
        `repr=False` back to `repr=True` does not silently restore the
        leaking one either. Only deleting *both* re-opens it, which is what
        `tests/unit/test_ports_source.py` is there to catch.
        """
        rendered = {item.name: getattr(self, item.name) for item in fields(self)}
        rendered["url"] = _redacted(self.url)
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
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.authenticated and not self.reachable:
            raise ValueError("a source cannot be authenticated without being reachable")
        if self.push_available and not self.authenticated:
            raise ValueError("push cannot be available without being authenticated")


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

        Must agree with `events()`: if this is `False`, `events()` raises
        `SourceNotSupported`; if it is `True`, `events()` yields a channel.
        An adapter that advertises push it does not have makes the
        reconciler skip a source it is the only cover for.
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
        """Walk the library, or only items changed since a cursor.

        Contract an implementation must guarantee:
        - `since` is inclusive: an item changed exactly at `since` is
          included, never dropped at the boundary.
        - No ordering is promised across items; callers must not rely on
          one.
        - The same item may be yielded more than once in a single walk
          (e.g. a paginated upstream listing whose pages overlap); callers
          deduplicate by `external_id`.
        - **Must stream, not materialise.** One upstream page may be held
          at a time; the walk may not build the library into a list first.
          The deployment this was built for holds 94,395 movies across 17
          libraries.
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
    def watch_state(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor.

        Same `since`-inclusivity, no-ordering, possible-duplicates,
        must-stream, and must-raise-never-truncate contract as
        `list_items`.

        Emits a state for every item the walk covers, including states that
        are entirely zero. Filtering those out looks like an obvious saving
        and is a correctness bug: un-marking something played *is* an
        all-zero state, so an implementation that skipped them could never
        propagate a reset — the delta walk would find the changed item and
        then discard exactly the record describing the change.
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
        reconciler covers them. Must agree with `supports_push`."""

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
