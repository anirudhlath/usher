"""Where each page of a `StartIndex` walk starts, and when the walk ends."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far."""

    fresh: list[dict[str, Any]]
    ended: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only, the one asked to count. The walk
    ends on an empty page, or on a short page once the cursor has reached that
    total. Short means shorter than the longest page served so far, or than
    `limit` before any has been: a server that caps `Limit` serves every page
    short of `limit`, and a library that grows during the walk keeps serving full
    pages past its old total, capped or not.
    """

    def __init__(self, *, limit: int, start: int) -> None:
        self.limit = limit
        self.start = start
        self.total: int | None = None
        self._cursor = start
        self._first = True
        self._longest = 0

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        # `> 0`, not `>= 0`: 0 is what a listing not asked to count reports. Not a
        # `bool` either, because JSON `true` is an `int` to Python.
        if self._first and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self.total = total
        self._first = False
        self._cursor = self.start + len(entries)
        reached = self.total is not None and self._cursor >= self.total
        # Against the longest page served, or `limit` before the first: a capped
        # server serves nothing longer, and its full pages past a stale total are a
        # library that grew.
        short = len(entries) < (self._longest or self.limit)
        self._longest = max(self._longest, len(entries))
        ended = not entries or (short and reached)
        return Page(fresh=[entry for entry in entries if isinstance(entry, dict)], ended=ended)

    def advance(self) -> int:
        """The next request's `StartIndex`."""
        self.start = self._cursor
        return self.start
