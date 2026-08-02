"""PRD 10's `usher.sse.connections`.

**A metric that is documented and never emitted is a dashboard panel that is
permanently empty, and nothing distinguishes that from a healthy zero.** So
every case here drives the real bus rather than a stub reader, and reads the
value back out of an `InMemoryMetricReader` -- asserting that the instrument
*exists* would pass against a `create_observable_gauge` nobody ever feeds.

The row is deliberately *not* ticked in PRD 10. The instrument ships and is
pinned here; what does not exist yet is the caller -- nothing in a running
process registers this reader until `create_app` builds the bus and starts
its lanes, so no deployment emits it today.
"""

from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from usher.services.events import InMemoryEventBus
from usher.telemetry import _observe_sse_connections, register_sse_gauge

_NO_DATA = type("_NoData", (), {"resource_metrics": ()})()


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _points(reader: InMemoryMetricReader, name: str) -> list[float]:
    return [
        float(getattr(point, "value", 0))
        for resource in (reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


async def test_the_gauge_reports_the_buses_live_subscriber_count(
    meter_reader: InMemoryMetricReader,
) -> None:
    """A live read, which is the one shape an observable callback may safely
    take in this project: `len()` on an in-memory set has no coroutine to
    bounce onto the event loop from the metric reader's background thread.
    `register_queue_gauges` documents why the queue's equivalent cannot."""
    bus = InMemoryEventBus()
    register_sse_gauge(lambda: bus.subscribers)
    assert _points(meter_reader, "usher.sse.connections") == [0.0]
    async with bus.subscribe(), bus.subscribe():
        assert _points(meter_reader, "usher.sse.connections") == [2.0]
    assert _points(meter_reader, "usher.sse.connections") == [0.0]


def test_the_series_is_a_gauge(meter_reader: InMemoryMetricReader) -> None:
    """PRD 10 documents a gauge. A row emitted under its documented *name*
    but the wrong *type* is the same class of failure as a near-miss name --
    the panel exists, the series is wrong, and nothing says so."""
    register_sse_gauge(lambda: 0)
    kinds = {
        metric.name: type(metric.data).__name__
        for resource in (meter_reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert kinds["usher.sse.connections"] == "Gauge"


def test_registering_a_second_reader_replaces_the_first(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The SDK keeps only the *first* observable instrument registered under
    a name and silently discards the rest, so a second `create_app()` in one
    process would otherwise leave the first, now-dead bus reporting its
    subscriber count forever."""
    register_sse_gauge(lambda: 3)
    _points(meter_reader, "usher.sse.connections")
    register_sse_gauge(lambda: 9)
    assert _points(meter_reader, "usher.sse.connections") == [9.0]


def test_no_reader_reports_no_observation_rather_than_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fabricated zero is a claim this process does not have. Pinned by
    calling the callback directly with the reader unset, for the reason M4
    recorded for the queue gauges: the branch is unreachable through
    `register_sse_gauge`, which assigns the reader *before* it creates the
    instrument."""
    monkeypatch.setattr("usher.telemetry._sse_reader", None)
    assert list(_observe_sse_connections(CallbackOptions())) == []
