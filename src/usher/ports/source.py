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
    """One item's watch state as a source reports it.

    **`play_count` and `last_played_at` are three-valued, and `None` is not
    zero.** `None` means "this read could not determine it"; `0` and a real
    datetime are positive claims a caller must honour, including the claim
    that something has been reset to unplayed.

    That distinction is not fastidiousness. Verified 2026-07-31 against Emby
    4.9.5.0: a `GET /Users/{user}/Items` *listing* reports `PlayCount: 0`
    and omits `LastPlayedDate` entirely, for the very item whose
    `GET /Users/{user}/Items/{item}` reports `PlayCount: 2` and a real
    `LastPlayedDate`. `PlaybackPositionTicks` and `Played` are correct in
    both. No `Fields` value, no `EnableUserData`, and no `Ids` restriction
    changes it. So `watch_state()` — which walks listings — cannot carry
    play history, and if these two fields could only say "0" then every
    delta walk would overwrite the household's real history with zeros,
    silently, forever.

    An implementation whose listing *does* carry history is free to report
    it from the walk; the contract requires only that a reported number be
    true, never that it be present. `get_watch_state` is where a caller goes
    when it needs the answer rather than whatever the cheap read happened to
    know. See [ADR-0014](../../../docs/prd/decisions/0014-absence-is-not-zero.md).
    """

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
    """One thing a source's push channel said changed.

    **Settled in M5** — this DTO used to carry a 🔶 asking whether a
    `WATCH_STATE_CHANGED` event should re-walk `watch_state(since=...)` to
    discover what changed, or carry the payload the upstream already sent.
    It carries the payload, and the two candidates were never close:
    `watch_state(since=...)` is a paged listing walk whose only knob is the
    cursor, measured at 29,027 items over a 30-day `MinDateLastSavedForUser`
    window against the one real deployment. Per event, on a lane PRD 01
    budgets at one connection per source.

    `external_ids` is the authoritative list of affected items.
    `watch_states` is the subset the adapter was able to parse out of the
    upstream's own message, **keyed by `external_id` rather than aligned by
    position** — an id in the first and not the second is a state the caller
    must fetch with `get_watch_state`, and aligning by position would let
    one unparseable entry write every later state onto the wrong item.
    `__post_init__` makes that a property of the DTO rather than a sentence
    here: a state naming an item the event did not is refused, so an adapter
    that built the two tuples out of different sets of message entries fails
    at construction instead of merging one item's state onto another's row.
    Unreachable from any payload on the Emby path — `UserDataChanged` has
    one `UserDataList` and both tuples come from its entries — so this
    refuses adapter bugs, not sources.

    **A carried state's `play_count`/`last_played_at` obey ADR-0014 exactly
    as a walk's do.** An adapter reports a number only if it is true. On
    Emby they are `None`: a `UserDataChanged` message is a third payload
    shape (a listing is one, an item route is another) and no run in this
    repository has ever parsed one, so absence is the honest answer and the
    `WATCH_HISTORY` backfill recovers the pair from the single-item route.
    A reported `0` is a positive claim that `merge_from_source` writes, so
    guessing one here overwrites real play history permanently.

    The item kinds carry no payload at all — Emby's `LibraryChanged` sends
    ids, not items — so `ITEM_ADDED`/`ITEM_UPDATED` are resolved with
    `get_item` per id, bounded by the caller.
    """

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
    """A URL cut at its query string, for rendering in a `repr` or a log line.

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

    **Public, and it has two callers.** `StreamTarget.__repr__` below, and
    `usher.adapters.emby.push`, whose socket URL is
    `/embywebsocket?api_key=<token>&deviceId=<id>` — the same token, in the
    same shape, one milestone later. One rule rather than two that can
    disagree; ADR-0012 records why it is spelled this way. It stays in this
    module rather than moving to a utility package because it is a property
    of this port's DTOs, and adapters may import `ports`.
    """
    cut = min((index for index in (url.find("?"), url.find("#")) if index != -1), default=-1)
    return url if cut < 0 else f"{url[:cut]}<redacted>"


# The scheme a deep-link target opens the client with. Kept under this exact
# name across the move D2 made -- see `wrap_deep_link` below -- because a
# second spelling of one constant is exactly what the move exists to
# prevent, and an earlier draft of this task's own plan used two names for
# it in two paragraphs.
INFUSE_SCHEME = "infuse"


def wrap_deep_link(inner_url: str) -> str:
    """Wrap a URL as an Infuse `x-callback-url` deep link.

    Moved here from `usher.adapters.emby.playback` in M9 (D2), ahead of the
    playback ticket ADR-0012 names as its M9 successor. That ticket is
    handed to "a third-party player that follows the redirect" -- a custom
    scheme is not something an HTTP `302` can produce, so whatever wraps a
    URL for Infuse has to run *after* the ticket exists, on the ticket's own
    URL, rather than on the source's direct-play one. The function that
    mints a ticket is therefore what has to call this, and that rules out
    `usher.adapters.emby.playback`: import contract 6 forbids
    `usher.services`/`usher.api` naming `usher.adapters.emby`, so a wrapper
    only that module offered would be unreachable from the code that mints
    the ticket.

    Lives beside `redact_query` for the identical reason that one is here
    rather than in a utility package: it is a property of this port's DTOs
    -- the string this returns is exactly `StreamTargetKind.DEEP_LINK`'s
    `url` -- and adapters may import `ports`. **It is a move, not a
    rewrite**: `build_stream_targets` calls this on the direct target's
    `url` and the result is unchanged, byte for byte, from what it built
    when the string was assembled inline.
    """
    return f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(inner_url, safe='')}"


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
    # `None` means "not determined", exactly as `push_available` does, and
    # for a stronger reason: ADR-0012 accepts the risk that an operator
    # configures an administrator account, and its recorded mitigation is
    # guidance rather than code. A fabricated `False` here would make an
    # unperformed check look like a performed one, which is worse than the
    # unknown it replaces. From M5 the same token also opens a long-lived
    # push socket, which is why the check ships now rather than staying a
    # recommendation.
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
    """What an on-demand push probe learned.

    `upgraded` says a channel opened. **It is deliberately not the answer**
    — ADR-0004 measured a handshake against a *nonexistent path* upgrading
    and receiving `Sessions`, so an upgrade proves nothing about the path,
    the subscription, or a proxy in between. `delivering` is
    `supports_push` read after a bounded wait, which every honest
    implementation grounds in received messages; that is the answer. The
    two are reported separately rather than collapsed because "the upgrade
    failed" and "the upgrade worked and nothing came" are different
    problems with different fixes, and an operator needs to know which.

    `events` is the kinds that arrived, for an operator who wants to know
    *what* the channel is carrying rather than only that it is carrying.
    Empty with `delivering=True` is normal and is the common case on an
    idle library: Emby's periodic `Sessions` message maps to no event and
    is exactly what keeps the channel measurably alive.

    `detail` is a short operator-facing string built from a translated
    `UsherPortError`, never a URL and never a credential — the same rule
    `SourceStatus.detail` carries, and it matters more here because the
    channel's own URL holds a session token (ADR-0012).
    """

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
        """Whether this adapter has a live push channel right now, **and the
        answer must be grounded in messages received rather than in a socket
        being open.**

        PRD 03: when the socket can't be established (or drops and stays
        down after N reconnect attempts), the adapter reports this `False`
        and the reconciler's nightly walk covers the gap. Mirrors
        `usher.domain.source.Source.supports_push`, which this populates.

        ADR-0004 measured a WebSocket handshake against a *nonexistent path*
        upgrading and being held open, so "the connection object exists" is
        a state this must answer `False` for. A reverse proxy that forwards
        `Upgrade` and then buffers produces the same state without any help
        from the source.

        **The relationship to `events()` is one-way, and stating it the
        other way round was wrong.** This property is a *health* signal and
        `SourceNotSupported` is a *capability* one:

        - `True` here ⟹ `events()` yields a channel. An adapter that
          advertises push it does not have makes the reconciler skip a
          source it is the only cover for.
        - `events()` raising `SourceNotSupported` ⟹ this is `False`, and
          stays `False`; that adapter has no push channel at all.
        - **The converse does not hold.** An adapter that *has* a channel
          reports `False` from the moment it is opened until the first
          message arrives on it, which is the whole of the rule ADR-0004's
          caveat forces. A contract that asserted
          `events()-was-offered is supports_push` would forbid exactly the
          honest implementation.
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
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
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

        May report `play_count`/`last_played_at` as `None` — "this read
        cannot say" — and must, rather than reporting a zero, whenever the
        listing it walks does not carry them. See `SourceWatchState`.

        `start_index` resumes a walk that was interrupted. It is the number
        of records **this walk has already yielded**: an offset into the
        stream this method produces under the given `since`, never into the
        source's unfiltered set. An implementation that pages a server which
        filters before it offsets passes it straight through. **It is only
        meaningful under a stable order, which this port does not promise
        and an adapter may** -- the Emby adapter walks
        `SortBy=DateCreated,SortName` ascending over an immutable creation
        date, so its walked prefix does not reorder between attempts. An
        adapter with no stable order must ignore it rather than skip
        arbitrary records, and say so.
        """

    @abstractmethod
    async def get_watch_state(self, external_id: str) -> SourceWatchState | None:
        """Authoritative watch state for one item, including play history.

        The expensive, exact counterpart to `watch_state`'s cheap walk.
        Where the walk is permitted to report `play_count`/`last_played_at`
        as `None` ("this read cannot say"), this method must report the real
        values whenever the source holds them — an implementation that
        delegates to the walk is wrong on any source whose listing is
        lossier than its item route, which is the measured behaviour of the
        one source that exists.

        `None` means the item is gone from the source, exactly as
        `get_item` does, and for the same reason: a caller must never learn
        to tell a deletion from a 404. A transient failure to reach the
        source raises (e.g. `PortUnavailable`) and is never reported as
        `None` — conflating them would let a flaky network merge an
        all-zero state over a real one.

        Costed deliberately: one request per item, against a library
        measured at 1,126,674 items. No walk may call this. Its caller is a
        queued, bounded backfill over the items a walk has flagged as
        played-but-history-unknown.
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
        """How many times this adapter's push channel has re-**opened**.

        PRD 10's `usher.source.push.reconnects`, and it is on the port
        rather than on a ledger the lane supervisor reaches into because
        the supervisor holds a `SourceAdapter` and nothing more — reading
        an attribute the port does not promise would report a silent zero
        the day it is renamed.

        Concrete rather than abstract for the reason `probe_push` is, plus
        one this property has of its own: an adapter with **no** push
        channel has never reconnected, so `0` is its true answer rather
        than the fabricated zero `usher.telemetry._push_observations`
        refuses to emit. An adapter that *has* a channel must override it;
        `tests/unit/test_ports_source.py` checks structurally that both
        implementations do, because a forgotten override is indistinguishable
        from "it has not reconnected yet" in every behavioural test.

        Counted on the second and later **open**, never on a failure: a
        lane that failed to connect five times and then succeeded
        reconnected *once*, and a counter on the failure reports five and
        makes an unreachable source look like a flapping one — a different
        diagnosis with a different fix. Cumulative for the lane rather than
        per connection, which is what an adapter holding one ledger across
        reconnects buys.
        """
        return 0

    async def probe_push(self, *, timeout_seconds: float = 15.0) -> PushProbe:
        """Open the push channel, wait, and report **what arrived**.

        Concrete rather than abstract, and that is the point: the body below
        is calls to `events()` and `supports_push` and nothing else, so
        every adapter inherits ADR-0004's rule instead of re-deriving it —
        and re-deriving it wrongly is a one-line mistake
        (`return PushProbe(upgraded=True, delivering=True)`) that no test of
        that adapter's own would obviously catch.

        Never raises. Its callers are an operator's diagnostic
        (`usher push --probe`) and a status screen, and both exist to render
        a failure rather than to handle one — the same reason `verify()`
        returns a `SourceStatus` instead of raising.

        Bounded by wall time rather than by a message count: a channel that
        is working may legitimately deliver nothing during the probe if
        nothing changed, and the source's own periodic traffic is what
        separates that from a dead one.

        `dict.fromkeys` rather than a `set`, for the reason M4 uses it
        everywhere: it deduplicates *and* keeps arrival order, so a probe's
        output reads in the order the channel produced it.
        """
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
