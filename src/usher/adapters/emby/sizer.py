"""How many items one Emby adapter asks for in a listing page, and how that adapts."""

from collections import deque

from loguru import logger
from opentelemetry import metrics

_meter = metrics.get_meter("usher.source.emby")
# PRD 10's catalogue. Only `PageSizer.report()` writes it.
_page_size = _meter.create_gauge(
    "usher.source.listing.page_size",
    unit="1",
    description="Items one source's adapter asks for in a listing page",
)

# Counted hand-offs a climbing size is judged on.
TRIAL_HANDOFFS = 4
# How many times as fast a doubled size must hand items off to keep climbing.
GAIN = 1.10
# Counted hand-offs at a settled size before it tries the next doubling again.
REPROBE_AFTER = 100


class PageSizer:
    """The listing page size one adapter asks for, climbing to the fastest.

    It starts at its floor and doubles, never past its ceiling, while each doubling
    hands items off at least `GAIN` times as fast as the size before, judged on
    `TRIAL_HANDOFFS` hand-offs. The first that gains less settles it one size back. A
    size settled below the ceiling tries the next doubling again after `REPROBE_AFTER`
    hand-offs, and a failed listing halves it, never below the floor, and settles it.

    Measurements run in trials, numbered by `trial`. A climb, a settle to another size
    and a failure each start a new one, and so does a hand-off at a concurrency other
    than its trial's, which is not counted. A hand-off or a failure of a request made
    in an earlier trial is ignored.
    """

    def __init__(self, floor: int, ceiling: int, *, source: str) -> None:
        if floor < 1:
            raise ValueError(f"a page sizer needs a floor of at least one item, not {floor}")
        if ceiling < floor:
            raise ValueError(f"a page sizer's ceiling {ceiling} is below its floor {floor}")
        self._floor = floor
        self._ceiling = ceiling
        self._source = source
        self._labels = {"source": source}
        self._size = floor
        self._climbing = True
        # The size it last climbed from and the rate that size handed items off at.
        self._before: tuple[int, float] | None = None
        self._trial = 0
        self._concurrency: int | None = None
        # The trial's last `TRIAL_HANDOFFS` counted hand-offs, as (items, seconds).
        self._kept: deque[tuple[int, float]] = deque(maxlen=TRIAL_HANDOFFS)
        self._counted = 0

    @property
    def size(self) -> int:
        """What the next listing request asks for."""
        return self._size

    @property
    def trial(self) -> int:
        """The trial a listing request issued now belongs to."""
        return self._trial

    @property
    def floor(self) -> int:
        return self._floor

    @property
    def ceiling(self) -> int:
        return self._ceiling

    def handed_off(self, items: int, seconds: float, *, trial: int, concurrency: int) -> None:
        """A listing handed off a page of `items` new items, `seconds` after its last.

        `trial` is the trial of the request for the page it handed off before this one,
        and `concurrency` the listing limit at the hand-off. A climbing size is judged at
        its trial's `TRIAL_HANDOFFS`th counted hand-off, and a size settled below the
        ceiling climbs again at its `REPROBE_AFTER`th.
        """
        if trial != self._trial or seconds <= 0:
            return
        if self._concurrency is not None and concurrency != self._concurrency:
            self._before = None
            self._new_trial(concurrency)
            return
        self._concurrency = concurrency
        self._kept.append((items, seconds))
        self._counted += 1
        if self._climbing and self._counted == TRIAL_HANDOFFS:
            self._decide(self._rate())
        elif not self._climbing and self._size < self._ceiling and self._counted == REPROBE_AFTER:
            self._climb(self._rate())

    def failed(self, trial: int) -> None:
        """A listing request made in `trial` failed as an outage.

        Unless a later trial has begun, the size halves, never below the floor, and
        settles there.
        """
        if trial != self._trial:
            return
        to = max(self._floor, self._size // 2)
        if to != self._size:
            logger.info(
                "{source}'s listing pages drop from {size:,} to {to:,} items "
                "after a failed listing",
                source=self._source,
                size=self._size,
                to=to,
            )
        self._size = to
        self._climbing = False
        self._before = None
        self._new_trial()

    def report(self) -> None:
        """Write the size to PRD 10's gauge, which nothing else writes."""
        _page_size.set(self._size, self._labels)

    def _decide(self, rate: float) -> None:
        """Climb on a gain or with nothing to compare, holding at the ceiling; else settle back."""
        if self._before is not None:
            back, previous = self._before
            gained = rate >= GAIN * previous
            if not gained:
                logger.info(
                    "{source}'s listing pages settle at {to:,} items: "
                    "{size:,} gave {rate:,.0f} items/s against {before:,.0f}",
                    source=self._source,
                    to=back,
                    size=self._size,
                    rate=rate,
                    before=previous,
                )
                self._settle(back)
                return
        if self._size < self._ceiling:
            self._climb(rate)
        else:
            self._settle(self._size)

    def _climb(self, rate: float) -> None:
        """Double the size, never past the ceiling, remembering this size and its `rate`."""
        to = min(2 * self._size, self._ceiling)
        if self._before is None:
            logger.info(
                "{source}'s listing pages grow from {size:,} to {to:,} items "
                "({rate:,.0f} items/s a walker)",
                source=self._source,
                size=self._size,
                to=to,
                rate=rate,
            )
        else:
            logger.info(
                "{source}'s listing pages grow from {size:,} to {to:,} items "
                "({before:,.0f} → {rate:,.0f} items/s a walker)",
                source=self._source,
                size=self._size,
                to=to,
                before=self._before[1],
                rate=rate,
            )
        self._before = (self._size, rate)
        self._size = to
        self._climbing = True
        self._new_trial()

    def _settle(self, size: int) -> None:
        self._climbing = False
        self._before = None
        if size != self._size:
            self._size = size
            self._new_trial()

    def _new_trial(self, concurrency: int | None = None) -> None:
        self._trial += 1
        self._kept.clear()
        self._counted = 0
        self._concurrency = concurrency

    def _rate(self) -> float:
        """Items per second over the kept hand-offs."""
        return sum(items for items, _ in self._kept) / sum(seconds for _, seconds in self._kept)
