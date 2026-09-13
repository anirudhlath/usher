"""Built rows and composed screens, cached in process (PRD 06)."""

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
# `usher.row.cache.hits` are the near misses this pair invites, by analogy with
# `usher.row.build.duration` one module over.
from usher.telemetry import CACHE_HITS, CACHE_MISSES  # noqa: E402

# One household's row cache is `_MAX_ENTRIES` slugs.
_MAX_ENTRIES = 512


# How deep the stale-key queue goes before `schedule` starts dropping.
REFRESH_QUEUE_SIZE = 32


class Freshness(enum.StrEnum):
    """The three states a cache read can be in, once serving stale is a thing the reader may do.

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
        """Keys waiting for the lane.

        Read by cases, and by nothing in `src/`.
        """
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        """Keys a full queue refused.

        **Not a metric**, deliberately: PRD 10's table is maintained rather than
        aspirational, and a drop is a normal outcome under load rather than an event
        worth a series of its own -- what it costs is one hard miss, which
        `usher.cache.misses` already counts. Exposed so a case can assert the drop
        happened.
        """
        return self._dropped

    @property
    def pending(self) -> frozenset[uuid.UUID]:
        """Keys queued or being refreshed right now."""
        return frozenset(self._pending)

    def schedule(self, user: User) -> None:
        """Hand this household's key to the lane.

        Returns immediately, always.

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
        """The lane's end.

        Suspends until there is a key; **does not** clear the pending mark -- see the
        class docstring.
        """
        return await self._queue.get()

    def done(self, user_id: uuid.UUID) -> None:
        """The refresh over this key has finished, however it finished.

        Called from the lane's `finally`, so a refresh that raised still releases the
        key rather than wedging the household out of refreshes for the life of the
        process.
        """
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
        """Entries held, both halves.

        Read by the eviction case, and by `usher home` when it reports what a warm
        compose was served from.
        """
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
        """M7's read, unchanged.

        fresh or nothing, and an expired entry is a miss on the counter as well as in
        the answer.

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
        # **The row half has no grace window, and that is a scope decision rather than
        # an omission.** The refresh unit is a *screen*: one key, one household, one
        # entry per user, bounded by the `users` table -- and rebuilding a screen
        # rebuilds the rows under it.
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
        """Drop every cached row and screen naming one of these titles."""
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
        """Empty both halves.

        `usher home --repeat` calls this between runs, because a repeat that measured
        cache hits would report a number near zero and mean nothing.
        """
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
