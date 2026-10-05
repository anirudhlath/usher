"""How many listing requests one Emby adapter has in flight, and how that backs off."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics

_meter = metrics.get_meter("usher.source.emby")
# PRD 10's catalogue. Written after every listing page, never at construction,
# so an adapter that never lists leaves no reading behind.
_concurrency = _meter.create_gauge(
    "usher.source.listing.concurrency",
    unit="1",
    description="Listing requests one source's adapter may have in flight at once",
)

# Successes in a row that raise a dropped limit by one.
RAISE_AFTER = 10


class ListingLimit:
    """The cap on listing requests in flight, shared by every walker and read-ahead.

    It starts at its ceiling. An outage or a 429 drops it to one, and each run of
    `RAISE_AFTER` successes in a row raises it by one, back up to the ceiling. A
    page waiting for a slot holds no request; a raise lets a waiter in at once,
    without anyone leaving.
    """

    def __init__(self, ceiling: int, *, source: str) -> None:
        if ceiling < 1:
            raise ValueError(f"a listing limit needs room for one request, not {ceiling}")
        self._ceiling = ceiling
        self._labels = {"source": source}
        self._limit = ceiling
        self._in_flight = 0
        self._streak = 0
        self._wake = asyncio.Event()

    @property
    def limit(self) -> int:
        return self._limit

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """One of the limit's slots, held for the body of the `async with`."""
        while self._in_flight >= self._limit:
            self._wake.clear()
            await self._wake.wait()
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._wake.set()

    def failed(self) -> None:
        """A request failed as an outage or a 429: one at a time from here."""
        self._limit = 1
        self._streak = 0
        _concurrency.set(self._limit, self._labels)

    def succeeded(self) -> None:
        """A request succeeded; every `RAISE_AFTER`-th in a row raises the limit by one."""
        self._streak += 1
        if self._streak == RAISE_AFTER:
            self._streak = 0
            if self._limit < self._ceiling:
                self._limit += 1
                self._wake.set()
        _concurrency.set(self._limit, self._labels)
