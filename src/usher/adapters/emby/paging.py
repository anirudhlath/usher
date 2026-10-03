"""Where each page of a `StartIndex` walk starts, what in it is new, and when it ends."""

from dataclasses import dataclass
from typing import Any

# How far a page after the first reaches back into the one before. A deletion
# behind the cursor shifts every later item left by one, so without this the
# next page skips one item per deletion, and the sweep retracts a file that is
# still there.
PAGE_OVERLAP = 50


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far."""

    fresh: list[dict[str, Any]]
    ended: bool
    shifted: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only, the one asked to count. A page is
    short when it is shorter than the longest page served before it, or than
    `limit` before any. A walk ends on an empty page; on a short page once the
    cursor has reached that total; or on a short page that brought nothing new.
    The second rule keeps a server that caps `Limit` from ending a walk early,
    and keeps reading a library that grows past its old total, capped or not.
    The third ends a walk that deletions left short of its total, which would
    otherwise re-read its tail until the end.

    Each page after the first reaches back `PAGE_OVERLAP` items, clamped to half
    the page before it so that a short page still advances. An entry the
    previous page carried is dropped by its `Id`. A page whose request reached
    back for an id, yet which holds none of the previous page's, has shifted by
    at least the reach-back. The previous page's last id alone would not say so:
    that item may have left.
    """

    def __init__(self, *, limit: int, start: int) -> None:
        self.limit = limit
        self.start = start
        self.overlap = 0
        self.total: int | None = None
        self._cursor = start
        self._served = 0
        self._longest = 0
        self._previous: frozenset[str] = frozenset()
        self._served_ids: list[str | None] = []
        self._first = True

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        ids = [_id_of(entry) for entry in entries]
        fresh = [
            entry
            for entry, external_id in zip(entries, ids, strict=True)
            if isinstance(entry, dict)
            and (external_id is None or external_id not in self._previous)
        ]
        # With no id to reach back for, because the request reached back nothing or
        # over entries without one, nothing says the page moved. Judged against the
        # whole previous page, not the reach-back alone: items listed ahead of it can
        # push the reach-back out of a page that skipped nothing.
        reach = self._served_ids[len(self._served_ids) - self.overlap :]
        reached_for_an_id = any(external_id is not None for external_id in reach)
        shifted = reached_for_an_id and self._previous.isdisjoint(ids)
        # `> 0`, not `>= 0`: 0 is what a listing not asked to count reports. Not a
        # `bool` either, because JSON `true` is an `int` to Python.
        if self._first and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self.total = total
        self._cursor = self.start + len(entries)
        self._served = len(entries)
        reached = self.total is not None and self._cursor >= self.total
        # Against the longest page served, or `limit` before the first: a capped
        # server serves nothing longer, and its full pages past a stale total are a
        # library that grew.
        short = len(entries) < (self._longest or self.limit)
        drained = not self._first and short and not fresh
        self._longest = max(self._longest, len(entries))
        named = [external_id for external_id in ids if external_id is not None]
        self._previous = frozenset(named)
        self._served_ids = ids
        self._first = False
        ended = not entries or (short and reached) or drained
        return Page(fresh=fresh, ended=ended, shifted=shifted)

    def advance(self) -> int:
        """The next request's `StartIndex`, reaching back into the page just served."""
        self.overlap = min(PAGE_OVERLAP, self._served // 2)
        self.start = max(0, self._cursor - self.overlap)
        return self.start


def _id_of(entry: object) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("Id")
        if isinstance(value, str) and value:
            return value
    return None
