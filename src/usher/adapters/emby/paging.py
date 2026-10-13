"""Where each page of a `StartIndex` walk starts, what in it is new, and when it ends."""

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from usher.adapters.emby.mapping import parse_datetime

# How far a page after the first reaches back into the one before. A deletion
# behind the cursor shifts every later item left by one, so without this the
# next page skips one item per deletion, and the sweep retracts a file that is
# still there.
PAGE_OVERLAP = 50

# How far a walk asks again before a page that moved past the overlap, doubled
# for each further page that moved.
BACKUP = 2 * PAGE_OVERLAP

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def key_of(entry: object) -> int | None:
    """An entry's `DateCreated` in whole microseconds since the epoch, or `None`."""
    if not isinstance(entry, dict):
        return None
    created = parse_datetime(entry.get("DateCreated"))
    return None if created is None else (created - _EPOCH) // _MICROSECOND


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far.

    A page that `shifted` moved past the overlap: nothing in it is read.
    """

    fresh: list[dict[str, Any]]
    ended: bool
    shifted: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only. A page is short when it is shorter
    than the longest page served before it, or than `limit` before any. A walk ends
    on an empty page; on a short page once the cursor has reached that total; or on
    a short page that brought nothing new.

    Each page after the first reaches back `PAGE_OVERLAP` items, clamped to half the
    page before it, and an entry the previous page carried is dropped by its `Id`.
    A page that reached back for an id yet holds none of the previous page's has
    moved, unless a `keyed` window sees it start before the anchor, the last
    `DateCreated` read. A page that moved is not read: the walk asks again `BACKUP`
    items before it, doubling while pages keep moving, never before 0. A resumed
    window judges by `after`, the anchor its last page left, and by that alone until
    it reads a page. Falling keys inside a page mark a keyed listing unsorted, and
    keys judge nothing after that.

    A keyed window bounded by `until` ends on the page whose last key passes it.
    """

    def __init__(
        self,
        *,
        limit: int,
        start: int,
        keyed: bool = False,
        after: int | None = None,
        until: int | None = None,
    ) -> None:
        self.limit = limit
        self.start = start
        self.keyed = keyed
        self.until = until
        self.overlap = 0
        self.total: int | None = None
        self._cursor = start
        self._served = 0
        self._longest = 0
        self._previous: frozenset[str] = frozenset()
        self._served_ids: list[str | None] = []
        self._reach: list[str | None] = []
        self._reached = after is not None
        self._anchor = after
        self._backoff = 0
        self._unsorted = False
        self._first = True

    @property
    def cursor(self) -> int:
        """Where the last page read ends."""
        return self._cursor

    @property
    def anchor(self) -> int | None:
        """The last `DateCreated` key read: what a resumed window starts judging from."""
        return self._anchor

    @property
    def unsorted(self) -> bool:
        """Whether a keyed listing has served a page whose keys fall."""
        return self._unsorted

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        ids = [_id_of(entry) for entry in entries]
        known = [key for key in map(key_of, entries) if key is not None]
        if self.keyed and any(later < earlier for earlier, later in itertools.pairwise(known)):
            self._unsorted = True
        # `> 0`, not `>= 0`: 0 is what a listing not asked to count reports. Not a
        # `bool` either, because JSON `true` is an `int` to Python.
        if self._first and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self.total = total
        # Against the longest page served, or `limit` before the first: a capped
        # server serves nothing longer, and its full pages past a stale total are a
        # library that grew.
        short = len(entries) < (self._longest or self.limit)
        first, self._first = self._first, False
        self._longest = max(self._longest, len(entries))
        if self._moved(ids, known):
            self._backoff = self._backoff * 2 if self._backoff else BACKUP
            return Page(fresh=[], ended=False, shifted=True)
        self._backoff = 0
        fresh = [
            entry
            for entry, external_id in zip(entries, ids, strict=True)
            if isinstance(entry, dict)
            and (external_id is None or external_id not in self._previous)
        ]
        self._cursor = self.start + len(entries)
        self._served = len(entries)
        reached = self.total is not None and self._cursor >= self.total
        # A walk that deletions left short of its total would otherwise re-read its tail to the end.
        drained = not first and short and not fresh
        self._previous = frozenset(external_id for external_id in ids if external_id is not None)
        self._served_ids = ids
        if known:
            self._anchor = known[-1]
        passed = (
            self.until is not None
            and self.keyed
            and not self._unsorted
            and self._anchor is not None
            and self._anchor > self.until
        )
        ended = not entries or (short and reached) or drained or passed
        return Page(fresh=fresh, ended=ended, shifted=False)

    def _moved(self, ids: list[str | None], known: list[int]) -> bool:
        """Whether the page at `start` moved past everything the request reached back over."""
        if self.start == 0 or not self._reached:
            return False
        anchor = self._anchor if self.keyed and not self._unsorted else None
        reached_for_an_id = any(external_id is not None for external_id in self._reach)
        # With no id to look for, the anchor alone judges only a resumed window that has
        # read nothing yet. After a page is read, the reach-back re-reads the anchor's own
        # item or a tie with it, which would read as moved each time the walk came back.
        if not reached_for_an_id and (anchor is None or self._served_ids):
            return False
        # Judged against the whole previous page, not the reach-back alone: items
        # listed ahead of it can push the reach-back out of a page that skipped nothing.
        if not self._previous.isdisjoint(ids):
            return False
        return anchor is None or not known or known[0] >= anchor

    @property
    def next_start(self) -> int:
        """The `StartIndex` `advance` moves to, without moving: what resumes after the last page."""
        if self._backoff:
            return max(0, self.start - self._backoff)
        return max(0, self._cursor - min(PAGE_OVERLAP, self._served // 2))

    def advance(self) -> int:
        """The next `StartIndex`: back into the last page read, or before a page that moved."""
        start = self.next_start
        if self._backoff:
            self._reach = self._served_ids
            self._reached = True
        else:
            reach = self._cursor - start
            self._reach = self._served_ids[len(self._served_ids) - reach :]
            self._reached = reach > 0
        self.start = start
        self.overlap = self._cursor - self.start
        return self.start


def _id_of(entry: object) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("Id")
        if isinstance(value, str) and value:
            return value
    return None
