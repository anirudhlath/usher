"""Built rows and composed screens, cached in process (PRD 06).

**In-process. Per worker. Dies with the process.** A dict in the server, not
Redis -- and on the deployment this project ships that is one cache, because
`compose.yml` runs one `usher` service and its `CMD` runs one uvicorn worker. A
restart, a deploy or a crash empties it and the next request pays a full
compose. Three consequences of the day that stops being true, stated now rather
than discovered later:

- Two replicas serve screens composed at different instants. **Harmless** --
  both are inside the TTL, and PRD 06 already calls the screen a ~30 s artefact.
- Two replicas do **not** share invalidation. A `row.invalidated` published in
  worker A does not clear worker B's entry, so B serves a stale screen until its
  own TTL expires. **Bounded, not broken** -- 30 s -- and it is the reason a
  cross-process cache and the cross-process `EventPublisher` are the same
  change. Both are named rather than built.
- Cache effectiveness is **now observable**: `usher.cache.hits`/`.misses`
  (PRD 10), labelled `cache` = `row` / `screen`, recorded where the read
  happens -- `get_row`/`read_screen` -- so every future *reader* is counted
  rather than every future caller remembering to. An entry found expired
  counts as a **miss**, not a hit: it is a rebuild, the same population
  `usher.row.build.duration` measures. A *served-stale* entry is the one
  exception and is argued below.

**Serve-stale-while-refreshing is M9's and is here now**, in the two shapes M7
named as the ones it must not take. Not one task per stale key (unbounded, in no
concurrency table) and not `api/lanes.py`'s per-source granularity (bounded,
wrong axis): **one lane, draining one bounded deduplicating `RefreshQueue`, each
refresh on its own session through `composition.unit_of_work`, drop-on-full**.
M7's reasoning stands unchanged and is why the queue carries a frozen `User`
rather than anything request-scoped -- the request's session is committed and
closed by `get_session` when the handler returns, and sharing it with a task is
the `AsyncSession` concurrency hazard ADR-0025 refuses one layer up, with the
same "usually works" signature.

**Dropping is safe, not merely tolerable.** An entry past `TTL + grace` is a
hard miss, so a dropped refresh degrades to exactly the cost M7 already pays:
the next request rebuilds. That is what lets `schedule` be `put_nowait` and
therefore synchronous, and a synchronous handover is the strongest available
spelling of "the screen never waits on it" -- there is nothing to await, so
`await schedule(...)` is a mypy error rather than a case that has to be lucky.

**A stale serve is a `usher.cache.hits` point carrying `freshness="stale"`,
and both halves of that are decisions.** A *hit*, because the request was
served from the cache and paid no rebuild -- counting it a miss would make the
hit rate say a compose happened when none did, and the hit rate is the number a
dashboard reads as "requests that avoided a compose". But not a *plain* hit,
because a plain hit hides precisely what this feature trades away: the household
is looking at data older than the TTL. `freshness` is `fresh` | `stale` on the
hits counter only -- a miss served nothing and so has no freshness to report --
which keeps the pair at four series and puts the trade on a dashboard instead of
in a comment. PRD 10's metrics table carries the label.

**Not a port.** ADR-0001 warns about the cost of an ABC with one
implementation, and what bought `EventPublisher` its port -- three publishers
across a layering boundary with no shared contract -- is absent here. The second
implementation that would force one is a cross-process cache, arriving with the
cross-process `EventPublisher`, and it is named for the same reason
`ports/events.py` names `LISTEN/NOTIFY`.

**Keys carry the user, and not because there are several.** v1 has one (PRD
07's authentication seam: *"every route depends on `current_user`, which returns
the singleton default user"*), so a key without it would work today and serve
one household's screen to another the day auth lands -- silently, with no error,
no log line and no metric. What makes the key correct is that the `user_id`
comes from the request, through `DefaultUserIdDep`, so replacing that one
dependency remains the whole of adding authentication.

**Bounded, because the key space is not.** `because-you-watched-<seed>` is one
slug per seed, so the row half grows with the household's watch history and, in
time, with the catalog -- the same cardinality hazard as the `provider` metric
label one layer over, and here it is a leak, because expired entries are read
past rather than removed and the TTL therefore reclaims nothing. The screen half
is one entry per user and is bounded by the `users` table.

**The clock is injected**, so a case can step *onto* an expiry boundary rather
than past it. M5's mutation sweep recorded the `stale_after` `<=` -> `<`
mutation surviving because every case in that file stepped past.
"""

import asyncio
import enum
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.rows import BuiltRow
from usher.domain.watch import User

_meter = metrics.get_meter("usher.cache")
# PRD 10's names, byte for byte -- `usher.cache.hit`/`.miss` (singular) and
# `usher.row.cache.hits` are the near misses this pair invites, by analogy
# with `usher.row.build.duration` one module over. **`cache`'s vocabulary is
# `row` and `screen` today, and a new cache appends its value in the commit
# that ships it** -- stated as a rule rather than a closed list because
# group C's image proxy is the third and writes its own.
#
# **Public, because the third cache arrived and re-declaring the pair is the
# one thing it must not do.** `services/images.py` records the image proxy's
# reads through *these two objects*: two `create_counter` calls under one
# meter for one instrument name is either a duplicate-instrument warning or a
# second stream, and either way a dashboard's hit rate silently stops covering
# a cache. The description therefore names no cache in particular -- it did
# say "Row/screen" until the proxy landed, which was accurate when this pair
# had one caller and a lie the moment it had two.
# Declared in `usher.telemetry` and re-exported here, because
# `services/images.py` also records through them and importing them from this
# module made a cycle. See the note beside their definition.
from usher.telemetry import CACHE_HITS, CACHE_MISSES  # noqa: E402

# One household's row cache is `_MAX_ENTRIES` slugs. Ten providers propose
# roughly a dozen rows a screen, so this is ~80 screens' worth of distinct
# `because-you-watched-<seed>` and `franchise-<id>` slugs before anything is
# evicted -- generous for a 30 s screen and small enough that the dict cannot
# become a leak. Not a `Settings` field, for the reason `_MAX_ROWS` is not.
#
# **The tenth provider adds a second way to mint a slug nothing will ask for
# again, and the sizing above does not model it.** A curated slug is zero-padded
# to the width of its generation (`domain/curation.py`), so a household whose
# generation crosses a power of ten re-mints every shelf under a new name and
# orphans the old width's entries -- at most `MAX_CURATED_ROWS` of them, once,
# per crossing. Against 512 and a 5-minute curated TTL that is noise, which is
# why the number does not move; recorded so the next person to re-derive the
# budget does not have to rediscover the term.
_MAX_ENTRIES = 512


# How deep the stale-key queue goes before `schedule` starts dropping. **This
# is the whole of the refresh path's bound**, alongside the one lane that
# drains it, and PRD 01's concurrency table quotes both -- a background refresh
# with no ceiling is how a cache stampede melts the box, and "bounded" that an
# operator cannot read a number for is not bounded in any useful sense.
#
# 32 rather than something larger: the key is a `user_id`, v1 has one household
# (PRD 07's authentication seam), and even a hundred would make a full queue
# unreachable for reasons that have nothing to do with the bound working. What
# the number really guards is the day a key space grows -- at which point the
# drop counter, not a crash, is what says so.
REFRESH_QUEUE_SIZE = 32


class Freshness(enum.StrEnum):
    """The three states a cache read can be in, once serving stale is a thing
    the reader may do.

    `ABSENT` covers both "nothing stored" and "past `TTL + grace`", because a
    reader has the same answer for them: rebuild. They are distinguished only
    in the dict, where one of them also deletes.
    """

    FRESH = "fresh"
    STALE = "stale"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class ScreenRead:
    """What `read_screen` found, and how old it is.

    A pair rather than `tuple[BuiltRow, ...] | None`, because the caller's
    decision -- serve and schedule a refresh, versus serve and do nothing --
    turns on the freshness and not on the value, and an empty screen `()` is a
    legitimate stored value that falsiness would confuse with a miss.
    """

    freshness: Freshness
    screen: tuple[BuiltRow, ...] | None


@dataclass(frozen=True, slots=True)
class StaleScreen:
    """One key handed from a request to the refresh lane.

    **A frozen `User`, never the `RowContext` the request built.** That context
    holds ten repositories bound to the request's `AsyncSession`, which
    `get_session` commits and closes when the handler returns -- so carrying
    one would hand a background task either a dead session or a live one shared
    with a request, which is the hazard ADR-0025 refuses one layer up. The user
    is the whole of what a refresh needs to build a context of its own, and the
    request already resolved it.

    `link` is the span context of whatever served the stale screen. The refresh
    runs as a **root** span with a `Link` back to it rather than as a child --
    PRD 10's rule for a worker's `job.*`, and for the same reason: the request
    has already returned, so a child span of a finished parent misstates
    causality.
    """

    user: User
    link: trace.SpanContext


class RefreshQueue:
    """Stale screen keys, bounded and deduplicated, on the way to one lane.

    **Deduplicated across the refresh, not just across the wait.** A key stays
    `pending` from `schedule` until the lane calls `done`, so a request
    arriving while a refresh is in flight schedules nothing. Cleared at `take`
    instead, every request in the refresh's own window would queue another full
    compose over the same household -- the stampede, arriving through the
    mechanism built to prevent it, and invisible to any case that only counts.

    **`schedule` is synchronous and never blocks.** `put_nowait` on a full
    queue raises rather than suspending, and the raise is turned into a
    dropped key: a request path that awaited `put` would block on exactly the
    load that filled the queue. Safe, because an entry past `TTL + grace` is a
    hard miss and the next request rebuilds -- the cost M7 already pays.
    """

    __slots__ = ("_dropped", "_pending", "_queue")

    def __init__(self, *, maxsize: int = REFRESH_QUEUE_SIZE) -> None:
        # Constructed outside a running loop by `create_app`, which is safe on
        # 3.13: `asyncio.Queue` binds its loop lazily on first use rather than
        # at construction. Same lifetime as the `RowCache` beside it -- one per
        # app, never per request.
        self._queue: asyncio.Queue[StaleScreen] = asyncio.Queue(maxsize=maxsize)
        self._pending: set[uuid.UUID] = set()
        self._dropped = 0

    @property
    def depth(self) -> int:
        """Keys waiting for the lane. Read by cases, and by nothing in `src/`."""
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        """Keys a full queue refused. **Not a metric**, deliberately: PRD 10's
        table is maintained rather than aspirational, and a drop is a normal
        outcome under load rather than an event worth a series of its own --
        what it costs is one hard miss, which `usher.cache.misses` already
        counts. Exposed so a case can assert the drop happened."""
        return self._dropped

    @property
    def pending(self) -> frozenset[uuid.UUID]:
        """Keys queued or being refreshed right now."""
        return frozenset(self._pending)

    def schedule(self, user: User) -> None:
        """Hand this household's key to the lane. Returns immediately, always.

        Returns `None` rather than a "was it queued" boolean on purpose: a
        caller that branched on the answer would be a request path making a
        decision about a background lane, and there is no correct second thing
        for `HomeService` to do. `depth`/`dropped`/`pending` are how a case
        sees which of the three outcomes happened.
        """
        if user.id in self._pending:
            return
        stale = StaleScreen(user=user, link=trace.get_current_span().get_span_context())
        try:
            self._queue.put_nowait(stale)
        except asyncio.QueueFull:
            self._dropped += 1
            return
        self._pending.add(user.id)

    async def take(self) -> StaleScreen:
        """The lane's end. Suspends until there is a key; **does not** clear
        the pending mark -- see the class docstring."""
        return await self._queue.get()

    def done(self, user_id: uuid.UUID) -> None:
        """The refresh over this key has finished, however it finished. Called
        from the lane's `finally`, so a refresh that raised still releases the
        key rather than wedging the household out of refreshes for the life of
        the process."""
        self._pending.discard(user_id)
        self._queue.task_done()


@dataclass(frozen=True, slots=True)
class _Entry[T]:
    value: T
    expires_at: datetime


class RowCache:
    """Built rows and composed screens, keyed by user, expiring by clock."""

    def __init__(
        self,
        *,
        clock: Callable[[], AwareDatetime],
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        self._now = clock
        self._max_entries = max_entries
        self._rows: dict[tuple[uuid.UUID, str], _Entry[BuiltRow]] = {}
        self._screens: dict[uuid.UUID, _Entry[tuple[BuiltRow, ...]]] = {}

    @property
    def size(self) -> int:
        """Entries held, both halves. Read by the eviction case, and by
        `usher home` when it reports what a warm compose was served from."""
        return len(self._rows) + len(self._screens)

    def read_screen(self, user_id: uuid.UUID, *, grace: timedelta = timedelta(0)) -> ScreenRead:
        """The three-state screen read: fresh, stale-inside-`grace`, or absent.

        **The grace is the caller's**, not a property of the dict, because the
        only caller entitled to a stale answer is one that can arrange for the
        entry to be replaced. `HomeService` passes `SCREEN_STALE_GRACE` when it
        holds a refresher and zero when it does not, which is what makes
        "served stale and never refreshed" unreachable rather than merely
        unlikely. At `grace=0` this is byte-for-byte M7's behaviour, which is
        what `get_screen` below still is.

        The boundaries are both `>=`-shaped and both are stepped exactly onto
        by a case: an entry *at* `expires_at` is expired, and an entry at
        `expires_at + grace` is a hard miss. M5's sweep recorded the
        `stale_after` `<=` -> `<` mutation surviving because every case in that
        file stepped past its boundary rather than onto it.
        """
        entry = self._screens.get(user_id)
        if entry is None:
            CACHE_MISSES.add(1, {"cache": "screen"})
            return ScreenRead(freshness=Freshness.ABSENT, screen=None)
        if not self._expired(entry):
            CACHE_HITS.add(1, {"cache": "screen", "freshness": "fresh"})
            return ScreenRead(freshness=Freshness.FRESH, screen=entry.value)
        if self._now() < entry.expires_at + grace:
            # A hit, because the request was served without a rebuild -- and
            # labelled, because a stale serve counted as a plain hit hides the
            # one thing this feature trades away. The module docstring argues
            # both halves; PRD 10's table carries the label.
            CACHE_HITS.add(1, {"cache": "screen", "freshness": "stale"})
            return ScreenRead(freshness=Freshness.STALE, screen=entry.value)
        # Removed on read rather than left: a screen past its grace is a row of
        # dead weight per user, and the `users` table is the only thing
        # bounding this half.
        self._screens.pop(user_id, None)
        # A rebuild, the same population `usher.row.build.duration` measures.
        # Recorded here rather than on `put_screen`, because the write that
        # repairs a miss is not a second event.
        CACHE_MISSES.add(1, {"cache": "screen"})
        return ScreenRead(freshness=Freshness.ABSENT, screen=None)

    def get_screen(self, user_id: uuid.UUID) -> tuple[BuiltRow, ...] | None:
        """M7's read, unchanged: fresh or nothing, and an expired entry is a
        miss on the counter as well as in the answer.

        Kept beside `read_screen` rather than folded into it because a reader
        that cannot refresh must not be handed a stale screen, and because the
        no-grace path is the one every caller outside `HomeService` wants. It
        is `read_screen(grace=0)` exactly -- one implementation, so the two
        cannot drift on the boundary they share.
        """
        read = self.read_screen(user_id)
        return read.screen if read.freshness is Freshness.FRESH else None

    def put_screen(
        self, user_id: uuid.UUID, screen: tuple[BuiltRow, ...], *, ttl: timedelta
    ) -> None:
        self._screens[user_id] = _Entry(value=screen, expires_at=self._now() + ttl)

    def get_row(self, user_id: uuid.UUID, slug: str) -> BuiltRow | None:
        key = (user_id, slug)
        entry = self._rows.get(key)
        if entry is None or self._expired(entry):
            self._rows.pop(key, None)
            CACHE_MISSES.add(1, {"cache": "row"})
            return None
        # **The row half has no grace window, and that is a scope decision
        # rather than an omission.** The refresh unit is a *screen*: one key,
        # one household, one entry per user, bounded by the `users` table --
        # and rebuilding a screen rebuilds the rows under it. A per-row grace
        # would need a per-row refresh to go with it, over a key space that is
        # `because-you-watched-<seed>` and therefore the household's watch
        # history; without one, a stale row served inside a screen is a row
        # nothing ever replaces, which is the failure serve-stale is supposed
        # to be the cure for.
        CACHE_HITS.add(1, {"cache": "row", "freshness": "fresh"})
        return entry.value

    def put_row(self, user_id: uuid.UUID, slug: str, row: BuiltRow, *, ttl: timedelta) -> None:
        self._rows[(user_id, slug)] = _Entry(value=row, expires_at=self._now() + ttl)
        self._evict()

    def invalidate(self, user_id: uuid.UUID, slugs: Iterable[str]) -> None:
        """Drop these rows for this household, **and its composed screen**.

        The screen goes too because it is a *composition of rows*: dropping the
        row and keeping the screen is the subtle half of the bug, since the next
        request is a screen cache hit and the invalidation had no visible effect
        at all.

        One household only. A cache that cleared everything on any invalidation
        would be correct and would make every other household pay for one
        household's play button.
        """
        for slug in slugs:
            self._rows.pop((user_id, slug), None)
        self._screens.pop(user_id, None)

    def invalidate_titles(self, title_ids: Iterable[uuid.UUID]) -> None:
        """Drop every cached row and screen naming one of these titles.

        **No `user_id`, and that is the difference from `invalidate` rather
        than an omission.** A play button is one household's act, so its
        invalidation is scoped to one household. Enrichment is a *catalog*
        write: the title it rewrote is equally stale on every screen holding
        it, and a per-household spelling would leave the second household
        serving the first's already-repaired staleness for the rest of a
        six-hour TTL.

        **Both halves are scanned.** A screen is stored whole, so a row can
        reach one without ever being written to the row half -- dropping only
        the row half is `invalidate`'s own recorded subtle bug (the next
        request is a screen cache hit and the invalidation had no visible
        effect) arriving through the other door.

        **An empty batch drops nothing**, which is the guard that keeps this a
        statement about titles: a job that enriched nothing hands over an empty
        sequence, and a `clear()` behind this name would empty a cache no write
        had staled while satisfying every case that names a title.

        Linear in the cache rather than indexed by title, deliberately. The row
        half is bounded at `_MAX_ENTRIES` and a screen is one household's eight
        rows, so the scan is a few thousand set probes; a title -> rows index
        would be a second structure maintained on every `put_row` for a dict
        whose entries all die within hours, which is the argument `_evict`
        already makes for not keeping an access record.
        """
        stale = frozenset(title_ids)
        if not stale:
            return
        emptied: set[uuid.UUID] = set()
        for key, entry in list(self._rows.items()):
            if any(card.title_id in stale for card in entry.value.cards):
                del self._rows[key]
                emptied.add(key[0])
        for user_id, screen in list(self._screens.items()):
            if user_id in emptied or any(
                card.title_id in stale for row in screen.value for card in row.cards
            ):
                del self._screens[user_id]

    def clear(self) -> None:
        """Empty both halves. `usher home --repeat` calls this between runs,
        because a repeat that measured cache hits would report a number near
        zero and mean nothing."""
        self._rows.clear()
        self._screens.clear()

    def _expired(self, entry: _Entry[object]) -> bool:
        """`>=`, so an entry *at* its expiry is expired.

        The boundary is asserted by a case that steps the clock exactly onto
        it, which is the habit M5's surviving `stale_after` mutation exists to
        teach: every case in that file stepped past the boundary, so `<` and
        `<=` agreed on every input the suite offered.
        """
        return self._now() >= entry.expires_at

    def _evict(self) -> None:
        """Hold the row half at `_max_entries`, soonest-to-expire first.

        Soonest-to-expire rather than least-recently-used: this cache has no
        access record, and adding one to implement LRU would be a second
        structure maintained on every read for a dict whose entries all die
        within hours anyway. Evicting the *newest* would be worse than a
        ceiling -- a cache that never serves what it was just asked to store,
        bounded and useless, its `usher.cache.hits` sunk near zero with no
        error anywhere else to say why.
        """
        if len(self._rows) <= self._max_entries:
            return
        ordered = sorted(self._rows.items(), key=lambda item: item[1].expires_at)
        for key, _ in ordered[: len(self._rows) - self._max_entries]:
            del self._rows[key]


__all__ = [
    "REFRESH_QUEUE_SIZE",
    "Freshness",
    "RefreshQueue",
    "RowCache",
    "ScreenRead",
    "StaleScreen",
]
