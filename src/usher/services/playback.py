"""Resolving how to play something, with a ticket in place of every URL.

The service behind `POST /titles/{id}/play` and `POST /episodes/{id}/play`,
and **the first thing in Usher whose honest answer can be "the source is down
and I cannot serve this from local state"**. `services/titles.py` says the
opposite about itself in as many words -- *"Nothing here calls a source, and
that is the whole design... This service cannot produce that failure, so there
is no status code to give a `code` to"* -- and this is the deliberate other
side: `stream_targets` needs the item's own `MediaSources` payload (container,
`MediaSourceId`), which is the network call, which is the 503.

**Three outcomes, not two**, and the route branches on the value rather than
on a message:

- `PLAYABLE` -- targets were found, *even if another source failed*. A partial
  degradation is still an answer, which is PRD 08's "a degraded subsystem
  narrows functionality; it never fails a request local state can answer".
- `UNAVAILABLE` -- nothing was found and at least one source could not answer.
- `NOT_PLAYABLE` -- nothing was found and every source that holds a copy
  answered. `SourceAdapter.stream_targets` documents `[]` as "no way to play
  this" (a series or season folder, a media source with no container) and
  explicitly not an error, and a household holding no copy is the ordinary
  case: the catalog holds 1,271,138 titles against one measured source's
  1,126,789 items.

**The mixed case is decided here rather than left to the route.** One source
answering `[]` while another is unreachable resolves to `UNAVAILABLE`, not
`NOT_PLAYABLE`: "you cannot play this" is a claim about the household's whole
holding, and a source that raised is a source that did not answer, so the
claim is unsupported. The retryable reply is the honest one.

**Ranking is the repository's, and this service never re-sorts.** Both
`MediaItemRepository.list_for_title` and `list_for_episode` promise `available`
first, then most recently seen, then `id` as a total-order tiebreak. Available
copies first is not an availability *filter*: PRD 02's soft delete means a
retracted copy very often still plays, and a household whose nightly sweep
over-retracted must not be told it owns nothing (ADR-0015). A second sort here
would be a second spelling of one rule, which is how two orderings come to
disagree; targets are concatenated in copy order, each copy's own ranking
(`stream_targets` answers "best first") preserved inside it.

**One adapter per copy, built through the injected factory and closed in a
`finally`** -- the exact path `SourceService.status` takes, whose comment is
the reason verbatim: *"one adapter is one connection pool, and a status
endpoint a dashboard polls would otherwise leak one per call"*. That costs one
`AuthenticateByName` per copy per play against an upstream PRD 01 measures at
1-5 s per request. Accepted, because it is the shape
`GET /admin/sources/{id}/status` already ships; `SourceRegistry`
(`composition.py`) already caches adapters for a registry's life and already
has `rebind`, but hoisting it onto `app.state` would couple a client route to
a background lane's lifetime and to the push lane's reconnect behaviour.
Named, not decided. **`EmbySession` mints a session per adapter**: M3 measured
that presenting a token with a different `DeviceId` neither forks nor
invalidates a session, and `Source.device_id` is persisted, so devices do not
accumulate -- sessions may, and that is worth watching in a live run.

**`Source.enabled` is deliberately not consulted.** It is how an operator parks
a server that is being rebuilt, and what it parks is the *background* work --
`api/lanes.py` drops a disabled source's push lane, and the sync selection
skips it. A client pressing play on a copy it can already see is foreground,
and refusing it would be a second, unstated meaning for one flag. A parked
server that really is down resolves to `UNAVAILABLE` with its name in the
detail, which is the answer an operator can act on.

**The detail an operator reads is a fixed sentence plus the source's name,
never `str(exc)`.** `SourceService.status` draws the same line for the same
reason: an upstream's own message quotes what it choked on, and what it choked
on here is a URL with a token in it (ADR-0012). `str(exc)` goes to the log line
and nowhere near a response.

**Ticket substitution is total, and keyed rather than positional.** Every
returned `url` is the injected `mint`'s answer, or a deep link wrapping one --
ADR-0012 records that `dataclasses.asdict`, `vars()`, `json.dumps` and
pydantic's `dump_json` all return `StreamTarget.url` in full, so a single
target left unsubstituted publishes the credential the ticket exists to hide.
A `DEEP_LINK` is matched to the `DIRECT` target **whose URL it contains**,
never to the one beside it in the list: this repository has recorded the
positional failure twice -- `SourceEvent.watch_states` is *"keyed by
`external_id` rather than aligned by position"*, and M5's `zip` of a matched
subset against a whole batch published item A's position under item B's id
(`services/push.py:203-219`). A deep link wrapping no direct URL this service
can see is **dropped, counted and logged**, because passing it through
publishes exactly the token the ticket hides. One ticket per distinct source
URL, memoised across the whole resolution, so both targets redeem the same
string.

**What `mint` is.** `Callable[[str], str]`, injected, so this service needs no
cipher and no knowledge of the redeem route's path -- `services/playback_
ticket.py` holds the primitive and the `/play` route owns the TTL and the URL
shape (ADR-0029). Whatever `mint` returns is substituted verbatim; if it
returns a whole `https://usher/stream/{ticket}` URL then that is what the deep
link wraps, percent-encoded by `wrap_deep_link` exactly as it encoded the
source URL before. Nothing here percent-encodes a ticket itself, so D1's
`quote(ticket, safe="=")` finding lands at the route that builds the path
segment and not in this module.

**Spans only, no metric.** PRD 10 puts spend and outcomes in SQL and names no
playback metric; M8's boundary call 7 is the precedent. No attribute here
carries a URL, a ticket or a token -- ADR-0012's "never a span attribute" is
about the source URL, and a ticket decrypts to one.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import quote

from loguru import logger
from opentelemetry import trace
from opentelemetry.trace import Span

from usher.domain.source import MediaItem, Source
from usher.ports.credentials import CredentialStore
from usher.ports.errors import UsherPortError
from usher.ports.repository import MediaItemRepository, SourceRepository
from usher.ports.source import (
    SourceAdapterFactory,
    StreamTarget,
    StreamTargetKind,
    wrap_deep_link,
)

__all__ = [
    "PlaybackResolution",
    "PlaybackService",
    "PlaybackStatus",
    "PlaybackTarget",
]

_tracer = trace.get_tracer("usher.playback")

# The whole of what a client is told about a source that could not answer.
# The names are appended; nothing derived from the upstream's own message
# ever is. See the module docstring.
_UNREACHABLE = "could not reach the source holding this item"


class PlaybackStatus(StrEnum):
    """Which of the three answers a resolution is.

    A value the route branches on, not a message: "the source is down" and
    "there is no way to play this" are different HTTP answers with different
    client behaviour, and a caller that had to read a sentence to tell them
    apart would be parsing prose. The RFC 9457 `code` each maps to belongs to
    the route, not here.
    """

    PLAYABLE = "playable"
    UNAVAILABLE = "unavailable"
    NOT_PLAYABLE = "not_playable"


@dataclass(frozen=True, slots=True)
class PlaybackTarget:
    """One ranked way to play, and the source it is served from.

    A wrapper rather than two more fields on `StreamTarget`: that is a port
    DTO an adapter builds, and it has no idea which configured source it is
    speaking for. PRD 07's `/play` example carries a `source` object, and a
    household with two copies has two -- so the attribution is per target
    rather than per response.

    `target.url` is a ticket, or a deep link wrapping one. Never a source URL:
    see the module docstring.
    """

    source_id: uuid.UUID
    source_name: str
    target: StreamTarget


@dataclass(frozen=True, slots=True)
class PlaybackResolution:
    """What the two `/play` routes answer with.

    `detail` is populated for `UNAVAILABLE` and is operator-facing: a fixed
    sentence and the names of the sources that could not answer, never an
    upstream's own message.
    """

    status: PlaybackStatus
    targets: tuple[PlaybackTarget, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        """Refuse the two states no route could render.

        The same shape `SourceStatus.__post_init__` uses and for the same
        reason: a playable answer with nothing to play, and a failed one
        carrying targets, are both bugs in whatever built this, and a caller
        branching on `status` would serve the wrong one of the two silently.
        """
        if bool(self.targets) is not (self.status is PlaybackStatus.PLAYABLE):
            raise ValueError(
                f"a {self.status.value} resolution cannot carry "
                f"{len(self.targets)} targets -- exactly PLAYABLE has them"
            )


class PlaybackService:
    def __init__(
        self,
        media_items: MediaItemRepository,
        sources: SourceRepository,
        credentials: CredentialStore,
        adapters: SourceAdapterFactory,
        mint: Callable[[str], str],
    ) -> None:
        self._media_items = media_items
        self._sources = sources
        self._credentials = credentials
        self._adapters = adapters
        self._mint = mint

    async def for_title(self, title_id: uuid.UUID) -> PlaybackResolution:
        """Ranked targets for a title, across every source that holds it."""
        with _tracer.start_as_current_span("playback.resolve") as span:
            span.set_attribute("usher.title_id", str(title_id))
            copies = await self._media_items.list_for_title(title_id)
            return await self._resolve(copies, span)

    async def for_episode(self, episode_id: uuid.UUID) -> PlaybackResolution:
        """Ranked targets for one episode.

        `list_for_episode`, not `list_for_title`: that read carries
        `AND episode_id IS NULL`, which is exactly what makes it useless here
        -- an episode's row is precisely one of the rows it excludes, and
        999,927 of the one measured library's 1,126,789 items are episodes.
        """
        with _tracer.start_as_current_span("playback.resolve") as span:
            span.set_attribute("usher.episode_id", str(episode_id))
            copies = await self._media_items.list_for_episode(episode_id)
            return await self._resolve(copies, span)

    async def _resolve(self, copies: Sequence[MediaItem], span: Span) -> PlaybackResolution:
        """Ask every copy's source, in the order the repository ranked them.

        One batched read of the source list rather than one `get` per copy --
        a household has sources in the single digits, and `TitleReadService`
        reads them the same way for the same reason.
        """
        span.set_attribute("usher.playback.copies", len(copies))
        sources = {source.id: source for source in await self._sources.list_all()}
        served: list[tuple[Source, StreamTarget]] = []
        # A dict rather than a list, for its ordered-set behaviour: two copies
        # on one unreachable source must name it once in the detail, in the
        # order the ranking met them.
        failed: dict[str, None] = {}
        for copy in copies:
            source = sources.get(copy.source_id)
            if source is None:
                # `media_items.source_id` is `ON DELETE CASCADE`, so a source
                # deleted between the two reads leaves a copy naming a row
                # that is already gone. Not a failure -- there is nothing to
                # build an adapter from, and reporting a 503 for a row on its
                # way out would be answering about the wrong thing.
                logger.debug(
                    "playback: copy {external_id} names a source that is gone",
                    external_id=copy.external_id,
                )
                continue
            targets = await self._copy_targets(source, copy)
            if targets is None:
                failed[source.name] = None
                continue
            served.extend((source, target) for target in targets)

        resolved = self._with_tickets(served)
        # Every served target becomes exactly one resolved target unless it
        # was dropped, so the difference *is* the drop count -- no second
        # tally to fall out of step with the loop that does the dropping.
        dropped = len(served) - len(resolved)
        if dropped:
            logger.warning(
                "playback: dropped {dropped} deep-link target(s) wrapping no visible direct url",
                dropped=dropped,
            )
        span.set_attribute("usher.playback.targets", len(resolved))
        span.set_attribute("usher.playback.sources_failed", len(failed))
        span.set_attribute("usher.playback.deep_links_dropped", dropped)

        if resolved:
            status, detail = PlaybackStatus.PLAYABLE, None
        elif failed:
            status = PlaybackStatus.UNAVAILABLE
            detail = f"{_UNREACHABLE}: {', '.join(failed)}"
        else:
            status, detail = PlaybackStatus.NOT_PLAYABLE, None
        span.set_attribute("usher.playback.status", status.value)
        return PlaybackResolution(status=status, targets=tuple(resolved), detail=detail)

    async def _copy_targets(self, source: Source, copy: MediaItem) -> list[StreamTarget] | None:
        """This copy's targets, or `None` if its source could not answer.

        `None` rather than a raise, because one copy failing is not the
        request failing -- the household's other source may hold the file,
        and `_resolve` needs to keep going.

        `except UsherPortError`, deliberately wide. `PortDataMalformed` from
        an unparseable payload, `PortAuthFailed` from a rotated password,
        `SourceNotSupported` from a factory with no implementation for this
        kind, and `PortDataMalformed` from a credential that no longer
        decrypts are all "this copy cannot be served"; narrowing the clause
        would let one of them escape the loop and turn a working second
        source into a 500.
        """
        try:
            credentials = await self._credentials.get(source.credentials_ref)
            if credentials is None:
                # Answered without building an adapter, exactly as
                # `SourceService.status` does: there is nothing to
                # authenticate with, so a probe could only spend a 1-5 s
                # round trip to learn what local state already knows.
                logger.warning(
                    "playback: source {source_id} has no stored credentials", source_id=source.id
                )
                return None
            adapter = self._adapters.build(source, credentials)
            try:
                return await adapter.stream_targets(copy.external_id)
            finally:
                # One adapter is one connection pool. In a `finally` because
                # the raising path is the one a client retries, and a leak
                # there compounds per attempt.
                await adapter.aclose()
        except UsherPortError as exc:
            # `str(exc)` here and nowhere else: an upstream's message quotes
            # the URL it choked on, and that URL carries a token (ADR-0012).
            logger.warning(
                "playback: source {source_id} could not serve {external_id}: {exc}",
                source_id=source.id,
                external_id=copy.external_id,
                exc=exc,
            )
            return None

    def _with_tickets(self, served: Sequence[tuple[Source, StreamTarget]]) -> list[PlaybackTarget]:
        """Substitute a ticket for every URL. See the module docstring.

        **Two passes, and that is what makes the pairing order-independent.**
        Every distinct direct URL is minted first, so a deep link that arrives
        *before* the target it wraps still finds its ticket -- an
        implementation that minted as it walked would depend on an adapter
        returning direct targets first, which no port promises.
        """
        minted: dict[str, str] = {}
        for _, target in served:
            if target.kind is StreamTargetKind.DIRECT and target.url not in minted:
                minted[target.url] = self._mint(target.url)
        resolved: list[PlaybackTarget] = []
        for source, target in served:
            if target.kind is StreamTargetKind.DIRECT:
                substituted = replace(target, url=minted[target.url])
            else:
                # Every non-direct kind is treated as wrapping, which is the
                # leak-safe default rather than an oversight: a fourth
                # `StreamTargetKind` added later either matches by
                # containment and is rebuilt, or is dropped. Passing an
                # unrecognised kind through unchanged is the one behaviour
                # that could publish a source URL.
                carried = _carried_url(target.url, minted)
                if carried is None:
                    continue
                substituted = replace(target, url=wrap_deep_link(minted[carried]))
            resolved.append(
                PlaybackTarget(source_id=source.id, source_name=source.name, target=substituted)
            )
        return resolved


def _carried_url(deep_link: str, minted: Mapping[str, str]) -> str | None:
    """Which minted source URL this deep link carries, or `None`.

    Containment rather than equality, because a deep link may legitimately
    carry more than the wrapped URL -- Infuse's `x-callback-url` form takes
    `x-success` and friends beside `url`. Both the percent-encoded and the
    raw spelling are checked: `wrap_deep_link` produces the first, and a URL
    that appeared raw would be a leak this must still catch rather than pass
    through.

    **The longest match, not the first.** One source URL can contain another
    -- two Emby session tokens where one is a prefix of the other produce
    exactly that, and there is no delimiter after the token -- so a first
    match would hand a deep link the wrong copy's ticket.
    """
    carried = [url for url in minted if quote(url, safe="") in deep_link or url in deep_link]
    return max(carried, key=len, default=None)
