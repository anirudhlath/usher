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
  happens -- `get_row`/`get_screen` -- so every future *reader* is counted
  rather than every future caller remembering to. An entry found expired
  counts as a **miss**, not a hit: it is a rebuild, the same population
  `usher.row.build.duration` measures.

**Serve-stale-while-refreshing is deliberately not implemented here**, and PRD
06's sentence is corrected rather than half-satisfied. A refresh task needs a
session it did not get from a request -- the request's own is committed and
closed by `get_session` when the handler returns, and sharing it with a task is
the `AsyncSession` concurrency hazard boundary call 8 refuses one layer up, with
the same "usually works" signature. Its own session is a connection outside the
request pool's accounting, per stale key, on demand. `api/lanes.py` is the right
mechanism and the wrong granularity: its lanes are one per *source*, enumerable
and bounded, where this would be one task per stale key with no lane in PRD 01's
concurrency table. And the payoff is one request per TTL per user, because every
other request in the window is already a hit. M9 owns it, alongside
`usher.cache.hits`/`.misses` -- a stale-while-revalidate path with no hit/miss
metric is a mechanism nobody can see working.

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

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from opentelemetry import metrics
from pydantic import AwareDatetime

from usher.domain.rows import BuiltRow

_meter = metrics.get_meter("usher.cache")
# PRD 10's names, byte for byte -- `usher.cache.hit`/`.miss` (singular) and
# `usher.row.cache.hits` are the near misses this pair invites, by analogy
# with `usher.row.build.duration` one module over. **`cache`'s vocabulary is
# `row` and `screen` today, and a new cache appends its value in the commit
# that ships it** -- stated as a rule rather than a closed list because
# group C's image proxy is the third and writes its own.
_cache_hits = _meter.create_counter(
    "usher.cache.hits", description="Row/screen cache reads that found a live entry"
)
_cache_misses = _meter.create_counter(
    "usher.cache.misses",
    description="Row/screen cache reads that found nothing or an expired entry",
)

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

    def get_screen(self, user_id: uuid.UUID) -> tuple[BuiltRow, ...] | None:
        entry = self._screens.get(user_id)
        if entry is None or self._expired(entry):
            # Removed on read rather than left: an expired screen kept in the
            # dict is a row of dead weight per user, and the `users` table is
            # the only thing bounding this half.
            self._screens.pop(user_id, None)
            # An expired entry is a miss, not a hit -- it is a rebuild, the
            # same population `usher.row.build.duration` measures. Recorded
            # here rather than on `put_screen`, because the write that
            # repairs a miss is not a second event.
            _cache_misses.add(1, {"cache": "screen"})
            return None
        _cache_hits.add(1, {"cache": "screen"})
        return entry.value

    def put_screen(
        self, user_id: uuid.UUID, screen: tuple[BuiltRow, ...], *, ttl: timedelta
    ) -> None:
        self._screens[user_id] = _Entry(value=screen, expires_at=self._now() + ttl)

    def get_row(self, user_id: uuid.UUID, slug: str) -> BuiltRow | None:
        key = (user_id, slug)
        entry = self._rows.get(key)
        if entry is None or self._expired(entry):
            self._rows.pop(key, None)
            _cache_misses.add(1, {"cache": "row"})
            return None
        _cache_hits.add(1, {"cache": "row"})
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


__all__ = ["RowCache"]
