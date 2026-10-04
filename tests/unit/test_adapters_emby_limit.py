"""`ListingLimit`: the listing requests in flight, dropped to one by a failure and raised back."""

import asyncio

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from usher.adapters.emby.limit import ListingLimit

SOURCE_NAME = "Shared Emby"


async def _turns(count: int = 20) -> None:
    """Let every task that can make progress make it."""
    for _ in range(count):
        await asyncio.sleep(0)


class _Holders:
    """Tasks that each take a slot and keep it until let go, logging both moments."""

    def __init__(self, limit: ListingLimit) -> None:
        self._limit = limit
        self._let_go: dict[int, asyncio.Event] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.log: list[tuple[str, int]] = []

    def start(self, number: int) -> None:
        self._let_go[number] = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._hold(number)))

    async def _hold(self, number: int) -> None:
        async with self._limit.slot():
            self.log.append(("in", number))
            await self._let_go[number].wait()
            self.log.append(("out", number))

    def let_go(self, number: int) -> None:
        self._let_go[number].set()

    def inside(self) -> set[int]:
        """The holders that have taken a slot and not yet given it back."""
        return {n for event, n in self.log if event == "in"} - {
            n for event, n in self.log if event == "out"
        }

    async def finish(self) -> None:
        for event in self._let_go.values():
            event.set()
        await asyncio.gather(*self._tasks)


def _caps(reader: InMemoryMetricReader) -> list[tuple[float, dict[str, str]]]:
    data = reader.get_metrics_data()
    points = [
        point
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "usher.source.listing.concurrency"
        for point in metric.data.data_points
    ]
    readings: list[tuple[float, dict[str, str]]] = []
    for point in points:
        assert isinstance(point, NumberDataPoint), "the cap is a gauge reading"
        attributes = {str(key): str(value) for key, value in dict(point.attributes or {}).items()}
        readings.append((point.value, attributes))
    return readings


async def test_a_new_limit_lets_its_ceiling_in_and_holds_the_next_until_one_leaves() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    holders = _Holders(limit)
    for number in range(4):
        holders.start(number)
    await _turns()
    assert holders.inside() == {0, 1, 2}, "three slots: three inside together, the fourth waiting"
    holders.let_go(1)
    await _turns()
    assert holders.inside() == {0, 2, 3}
    assert holders.log.index(("in", 3)) > holders.log.index(("out", 1))
    await holders.finish()


async def test_a_failure_lets_no_page_in_until_every_page_in_flight_has_left() -> None:
    """Dropped to one with three in flight, a fourth waits for all three, not for one."""
    limit = ListingLimit(3, source=SOURCE_NAME)
    holders = _Holders(limit)
    for number in range(3):
        holders.start(number)
    await _turns()
    assert holders.inside() == {0, 1, 2}, "the premise: three in flight when the failure lands"
    limit.failed()
    holders.start(3)
    holders.let_go(0)
    holders.let_go(1)
    await _turns()
    assert limit.limit == 1
    assert holders.inside() == {2}, "one still in flight, so the fourth still waits"
    holders.let_go(2)
    await _turns()
    assert holders.inside() == {3}
    await holders.finish()


async def test_ten_successes_in_a_row_raise_the_limit_by_one_up_to_its_ceiling() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    limit.failed()
    readings: list[int] = []
    for _ in range(30):
        limit.succeeded()
        readings.append(limit.limit)
    assert readings == [1] * 9 + [2] * 10 + [3] * 11


async def test_a_failure_starts_the_run_of_successes_again() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    limit.failed()
    for _ in range(9):
        limit.succeeded()
    limit.failed()
    readings: list[int] = []
    for _ in range(10):
        limit.succeeded()
        readings.append(limit.limit)
    assert readings == [1] * 9 + [2]


async def test_a_raised_limit_lets_a_waiting_page_in_while_the_first_still_holds() -> None:
    """Nobody has left, so only the raise itself can let the waiter in."""
    limit = ListingLimit(2, source=SOURCE_NAME)
    limit.failed()
    holders = _Holders(limit)
    holders.start(0)
    holders.start(1)
    await _turns()
    assert holders.inside() == {0}, "the premise: one slot, so the second waits"
    for _ in range(10):
        limit.succeeded()
    await _turns()
    assert holders.inside() == {0, 1}
    await holders.finish()


async def test_the_limit_reports_its_cap_after_every_page_and_nothing_before_the_first() -> None:
    """An adapter built for a playback request never lists, so it never writes the series.

    One that lists reports its cap after each page, labelled by source, so an idle
    adapter built during a walk that backed off cannot paint over the walk's reading.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    limit = ListingLimit(3, source=SOURCE_NAME)
    assert _caps(reader) == [], "no page yet, so no reading"
    limit.succeeded()
    readings = [_caps(reader)]
    limit.failed()
    readings.append(_caps(reader))
    for _ in range(10):
        limit.succeeded()
    readings.append(_caps(reader))
    labels = {"source": SOURCE_NAME}
    assert readings == [[(3, labels)], [(1, labels)], [(2, labels)]]


def test_a_limit_with_no_room_is_refused() -> None:
    """A cap of zero would park every listing request forever."""
    with pytest.raises(ValueError, match="room for one"):
        ListingLimit(0, source=SOURCE_NAME)
