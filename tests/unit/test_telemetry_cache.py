"""`usher.cache.hits`/`.misses` -- PRD 10, M9.

**`services/rows/cache.py`'s own docstring said cache effectiveness is not
observable in M7, and `services/home.py:329` says why a histogram cannot
substitute**: a cache hit records no `usher.row.build.duration` point because
the cache returns before the timer opens, so that histogram's population is
misses only and a hit rate is not recoverable from it. These two counters are
the fix, and the read is where the counter goes -- inside `RowCache.get_row`
and `RowCache.get_screen` -- so every future *reader* is counted rather than
every future caller remembering to.

Driven through the real `HomeService`/`RowCache` pair rather than by calling
`counter.add` directly, so an instrument created at import and never recorded
to fails here -- same discipline as `test_telemetry_search.py`.

`tests/conftest.py::reset_otel_meter_provider` (autouse) is what makes
installing a fresh `MeterProvider` per case here work at all: `set_meter_
provider` is set-once and every `usher` module's counter is a `_Proxy*` shell
caching the first real instrument it is ever handed.
"""

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.unit.rows import Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import RowCard, RowFamily
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import HomeService
from usher.services.rows.cache import RowCache

_TTL = dt.timedelta(seconds=30)
_START = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC)


class _Clock:
    """A clock that only moves when a case moves it -- `test_services_rows_
    cache.py`'s own fixture, copied rather than imported so this file has no
    cross-file coupling to a sibling suite's internals."""

    def __init__(self) -> None:
        self.now = _START

    def advance(self, delta: dt.timedelta) -> None:
        self.now += delta

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


@pytest.fixture
def ctx() -> RowContext:
    return Library().context()


def _card(name: str) -> RowCard:
    return RowCard(
        title_id=uuid.uuid4(),
        kind=TitleKind.MOVIE,
        name=name,
        enrichment_state=EnrichmentState.SKELETON,
    )


def _provider(
    slug: str, *, family: RowFamily = RowFamily.SOURCE, ttl: dt.timedelta = _TTL
) -> FakeRowProvider:
    row = FakeRow(slug, family=family, ttl=ttl, cards=(_card(slug),))
    return FakeRowProvider(proposals=(ScoredRow(row=row, score=0.9),), slug_prefix=slug)


def _recorded(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, object], float]]]:
    data = reader.get_metrics_data()
    found: dict[str, list[tuple[dict[str, object], float]]] = {}
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points = found.setdefault(metric.name, [])
                for point in metric.data.data_points:
                    # `usher.home.compose.duration`/`usher.row.build.duration`
                    # (histograms, `.sum`) are recorded on the same reader by
                    # the real `HomeService` this file drives its cases
                    # through; only the two counters this file is about carry
                    # `.value`.
                    raw = getattr(point, "value", None)
                    if raw is None:
                        raw = getattr(point, "sum", 0)
                    points.append((dict(point.attributes or {}), float(raw or 0)))
    return found


def _value(points: list[tuple[dict[str, object], float]], cache: str) -> float:
    for attrs, value in points:
        if attrs.get("cache") == cache:
            return value
    return 0.0


# `get_metrics_data()` is typed as optional and never is here.
_NO_DATA = type("_NoData", (), {"resource_metrics": ()})()


def _kinds(reader: InMemoryMetricReader) -> dict[str, str]:
    return {
        metric.name: type(metric.data).__name__
        for resource in (reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


# -- the failing test the plan names ---------------------------------------


async def test_a_warm_screen_records_a_hit_and_a_cold_one_records_a_miss(
    ctx: RowContext, clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """Compose twice through the real `HomeService`. The first call finds no
    screen cached -- a miss -- and warms it; the second finds it -- a hit.

    The wrong implementations this rules out: a counter recorded on `put_*`
    instead of on the failed `get_*` (which double-counts a rebuild -- a miss
    followed by the write that repairs it would be two events instead of
    one), and a `cache` label hard-coded to one value.
    """
    cache = RowCache(clock=clock)
    provider = _provider("recently-added")
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)
    after_first = _recorded(meter_reader)
    assert _value(after_first.get("usher.cache.misses", []), "screen") == 1.0
    assert _value(after_first.get("usher.cache.hits", []), "screen") == 0.0

    await service.compose(ctx)
    after_second = _recorded(meter_reader)
    assert _value(after_second.get("usher.cache.misses", []), "screen") == 1.0, (
        "the second compose must not record a second miss"
    )
    assert _value(after_second.get("usher.cache.hits", []), "screen") == 1.0


# -- both labels, and a case per cache value --------------------------------


async def test_a_cold_row_records_a_miss_labelled_row(
    ctx: RowContext, clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """The composer always checks the screen cache first, so the same compose
    that misses `cache="screen"` also misses `cache="row"` once it reaches
    `_build` -- this pins that the row half is labelled independently rather
    than folded into the screen's count."""
    cache = RowCache(clock=clock)
    provider = _provider("recently-added")
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)

    recorded = _recorded(meter_reader)
    assert _value(recorded.get("usher.cache.misses", []), "row") == 1.0
    assert _value(recorded.get("usher.cache.hits", []), "row") == 0.0


async def test_a_row_that_outlives_its_screen_records_a_hit_labelled_row(
    ctx: RowContext, clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """PRD 06 caches at two layers because a row's own TTL can outlive the
    ~30s screen -- `test_services_rows_cache.py`'s own
    `test_a_row_survives_the_screen_expiring_because_its_own_ttl_is_longer`
    is the composer-level proof; this is the metric-level one. The screen
    expires, forcing a rebuild pass, and that pass finds the row still live:
    a hit labelled `row`, alongside the second screen miss."""
    cache = RowCache(clock=clock)
    provider = _provider(
        "because-you-watched", family=RowFamily.SIMILARITY, ttl=dt.timedelta(hours=6)
    )
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)
    clock.advance(dt.timedelta(seconds=31))
    await service.compose(ctx)

    recorded = _recorded(meter_reader)
    assert _value(recorded.get("usher.cache.misses", []), "screen") == 2.0
    assert _value(recorded.get("usher.cache.hits", []), "row") == 1.0
    assert _value(recorded.get("usher.cache.misses", []), "row") == 1.0, (
        "one miss from the cold first compose, and no second miss from the warm row"
    )


# -- an expired entry is a miss, not a hit -----------------------------------


def test_an_entry_exactly_at_its_expiry_records_a_miss(
    clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """Stepped *onto* the boundary, not past it -- the habit M5's surviving
    `stale_after` `<=` -> `<` mutation exists to teach: every case that steps
    past the boundary leaves both spellings agreeing on every input offered.
    An entry at its expiry is a rebuild, so it must count as a miss."""
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, (), ttl=_TTL)

    clock.advance(_TTL)

    assert cache.get_screen(user) is None
    recorded = _recorded(meter_reader)
    assert _value(recorded.get("usher.cache.misses", []), "screen") == 1.0
    assert _value(recorded.get("usher.cache.hits", []), "screen") == 0.0


# -- the catalogue -----------------------------------------------------------


def test_the_two_series_are_counters(meter_reader: InMemoryMetricReader) -> None:
    """PRD 10 documents `usher.cache.hits`/`.misses` as counters, not gauges
    or histograms -- the distinction is the question answered: "how many
    reads landed" accumulates, it does not sample a current level."""
    cache, user = RowCache(clock=lambda: _START), uuid.uuid4()
    cache.get_screen(user)
    cache.put_screen(user, (), ttl=_TTL)
    cache.get_screen(user)

    kinds = _kinds(meter_reader)
    assert kinds["usher.cache.hits"] == "Sum"
    assert kinds["usher.cache.misses"] == "Sum"


def test_the_instruments_exist_at_import(meter_reader: InMemoryMetricReader) -> None:
    """A rename in `src/` that leaves `_cache_hits`/`_cache_misses` pointing
    at a near-miss name is a dashboard panel that is permanently empty and
    indistinguishable from a healthy zero -- caught here structurally rather
    than only through a case that happens to record to it."""
    import usher.services.rows.cache as cache_module

    names = {
        instrument._name
        for instrument in vars(cache_module).values()
        if isinstance(instrument, _ProxyInstrument)
    }
    assert {"usher.cache.hits", "usher.cache.misses"} <= names, names
