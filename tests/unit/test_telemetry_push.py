"""PRD 10's metric catalogue for M5's push lane.

**A metric that is documented and never emitted is a dashboard panel that
is permanently empty, and nothing distinguishes that from a healthy zero.**
M4 found three of PRD 10's rows in that state -- two gauges that did not
exist, one emitted under a different name than documented -- so every case
here drives the code that owns the instrument and reads the value back out
of an `InMemoryMetricReader`. Asserting an instrument *exists* would pass
against a `create_counter` nobody ever calls.

The last case reads the names off the instruments themselves, so a rename in
`src/` fails even if whoever renamed it also updated the case that drives it.
"""

import sys
import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.source import SourceEvent, SourceEventKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.push import PushApplyService
from usher.services.watch_sync import WatchStateSyncService
from usher.telemetry import (
    PushSnapshot,
    _observe_push_connected,
    _observe_push_reconnects,
    register_push_gauges,
)

# Every metric PRD 10 marks M5 and this group owes. Named here rather than
# discovered from the code, so a rename in `src/` fails this file instead of
# quietly moving a dashboard's target.
PRD_10_M5_PUSH_METRICS = frozenset(
    {
        "usher.source.push.events",
        "usher.source.push.connected",
        "usher.source.push.reconnects",
    }
)

# `get_metrics_data()` is typed as optional and never is here; named so the
# walk below reads as a walk rather than as a `None` check.
_NO_DATA = type("_NoData", (), {"resource_metrics": ()})()

SOURCE = Source(
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id="device-1",
)


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A real `MeterProvider` with an in-memory reader, installed for this
    test alone -- `tests/conftest.py::reset_otel_meter_provider` is what
    makes "for this test alone" true (the API refuses a second
    `set_meter_provider` in a process, and every module-level instrument
    caches the first real one it is handed)."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


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
                    raw = getattr(point, "value", None)
                    if raw is None:
                        raw = getattr(point, "sum", 0)
                    points.append((dict(point.attributes or {}), float(raw or 0)))
    return found


def _applier(events: FakeEventPublisher) -> PushApplyService:
    titles = FakeTitleRepository()
    matching = FakeTitleMatchRepository(titles)
    queue = FakeJobQueue()
    media_items = FakeMediaItemRepository()

    async def _commit() -> None:
        return None

    return PushApplyService(
        IngestService(
            matcher=MatchService(titles=titles, matching=matching, queue=queue),
            matching=matching,
            media_items=media_items,
            episodes=FakeEpisodeRepository(),
            queue=queue,
        ),
        WatchStateSyncService(
            media_items=media_items,
            watch_states=FakeWatchStateRepository(),
            runs=FakeSyncRunRepository(),
            queue=queue,
            commit=_commit,
        ),
        events,
        _commit,
    )


async def test_every_applied_event_is_counted_by_source_and_kind(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10's `usher.source.push.events`. Labelled by kind because the two
    that cost nothing (`item_removed`, which ADR-0015 forbids acting on) and
    the one that costs a merge look identical on an unlabelled series -- and
    "is this lane doing anything" is the question dashboard 3's push panel
    exists to answer alongside uptime.

    Counted on the way *out* of `apply`, so a deferred event is counted too:
    an event the lane answered with a delta walk is still an event the
    source pushed, and a series that dropped them would read as a quiet
    source during exactly the library scan that produced them.
    """
    applier = _applier(FakeEventPublisher())
    adapter = FakeSourceAdapter(SOURCE)
    await applier.apply(
        SOURCE,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1",)),
        user_id=uuid.uuid4(),
    )
    await applier.apply(
        SOURCE,
        adapter,
        SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=()),
        user_id=uuid.uuid4(),
    )
    recorded = _recorded(meter_reader)
    assert sorted(
        (str(attributes["kind"]), value)
        for attributes, value in recorded["usher.source.push.events"]
    ) == [("item_removed", 1.0), ("watch_state_changed", 1.0)]
    assert {
        str(attributes["source"]) for attributes, _ in recorded["usher.source.push.events"]
    } == {"Living Room Emby"}


def _instrument_names(reader: InMemoryMetricReader) -> set[str]:
    """Every instrument name this process has created, from two places.

    Module-level instruments (`_meter.create_counter(...)` at import) are
    reachable by walking `usher.*` for `_ProxyInstrument`s, which keep their
    name whether or not a real provider has resolved them yet. The two push
    gauges are not module-level -- `register_push_gauges` creates them -- so
    they come from the reader instead, after registering a reader that
    reports one series per instrument. Same split
    `tests/unit/test_telemetry_pipeline.py` makes for M4's catalogue.
    """
    names = {
        instrument._name
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("usher")
        for instrument in vars(module).values()
        if isinstance(instrument, _ProxyInstrument)
    }
    register_push_gauges(lambda: {"A": PushSnapshot(delivering=False, reconnects=0)})
    return names | set(_recorded(reader))


def test_every_prd_10_push_metric_actually_exists(meter_reader: InMemoryMetricReader) -> None:
    """The catalogue as a set, read off the instruments themselves rather
    than restated. Each name has its own case above that drives the code
    emitting it -- this is the one that fails when a rename in `src/` moves
    a dashboard's target, even if whoever renamed it also updated the case
    that drives it. PRD 10 already prices the failure: a metric emitted
    under a near-miss name is a permanently empty panel that nothing
    distinguishes from a healthy zero.
    """
    assert _instrument_names(meter_reader) >= PRD_10_M5_PUSH_METRICS


def test_the_module_owning_those_instruments_is_imported() -> None:
    """`_instrument_names` walks `sys.modules`, so a catalogue case whose
    module was never imported compares an empty set against a set it happens
    to contain and passes having measured nothing. Pinned rather than relied
    on -- the same family as "a harness must refuse to classify a run that
    did not run"."""
    assert "usher.services.push" in sys.modules


# -- the two series PRD 10 reserved for M5 ----------------------------------


def _points(reader: InMemoryMetricReader, name: str) -> list[tuple[float, str]]:
    return sorted(
        (value, str(attributes["source"])) for attributes, value in _recorded(reader).get(name, [])
    )


def test_the_push_gauge_reports_delivery_not_connection(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10's "Push down" alert fires on `push.connected == 0` for fifteen
    minutes. A gauge reporting the *socket* would be permanently green
    against the one failure ADR-0004 warns about -- a channel that upgraded,
    is held open, and delivers nothing -- which is precisely the condition
    that alert exists to catch."""
    register_push_gauges(lambda: {"Living Room Emby": PushSnapshot(delivering=False, reconnects=2)})
    assert _points(meter_reader, "usher.source.push.connected") == [(0.0, "Living Room Emby")]


def test_the_push_gauge_reports_one_for_a_delivering_channel(
    meter_reader: InMemoryMetricReader,
) -> None:
    register_push_gauges(lambda: {"Living Room Emby": PushSnapshot(delivering=True, reconnects=0)})
    assert _points(meter_reader, "usher.source.push.connected") == [(1.0, "Living Room Emby")]


def test_the_reconnect_series_reports_the_lanes_cumulative_count(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Cumulative for the adapter's whole life rather than per connection --
    `PushHealth` is one object across reconnects for exactly this. A
    per-connection counter would read 0 or 1 forever and dashboard 3's
    "reconnect count" panel would be a flat line."""
    register_push_gauges(lambda: {"A": PushSnapshot(delivering=True, reconnects=7)})
    assert _points(meter_reader, "usher.source.push.reconnects") == [(7.0, "A")]


def test_the_reconnect_series_is_a_counter_and_the_uptime_series_is_a_gauge(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10 documents one of each, and the two are different instruments
    on the wire: a monotonic `Sum` is what a Prometheus counter is, and
    `rate()` over a gauge is not the same query. A row emitted under its
    documented *name* but the wrong *type* is the same class of failure as a
    near-miss name -- the panel exists, the series is wrong, and nothing
    says so.

    Read off the exported data rather than off the call, so registering both
    as gauges fails here even if the case above still passes.
    """
    register_push_gauges(lambda: {"A": PushSnapshot(delivering=True, reconnects=3)})
    kinds = {
        metric.name: type(metric.data).__name__
        for resource in (meter_reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert kinds["usher.source.push.connected"] == "Gauge"
    assert kinds["usher.source.push.reconnects"] == "Sum"


def test_registering_a_second_reader_replaces_the_first(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The SDK keeps only the *first* observable instrument registered under
    a name and silently discards the rest -- verified directly for
    `register_queue_gauges` and true here for the same reason. A
    re-registration that only created a second instrument would leave the
    first, now-dead reader reporting forever."""
    register_push_gauges(lambda: {"A": PushSnapshot(delivering=False, reconnects=0)})
    _recorded(meter_reader)
    register_push_gauges(lambda: {"B": PushSnapshot(delivering=True, reconnects=1)})
    assert _points(meter_reader, "usher.source.push.connected") == [(1.0, "B")]


def test_no_reader_reports_no_observation_rather_than_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fabricated zero is indistinguishable from a source whose channel is
    down, and PRD 10's "Push down" alert fires on exactly that value -- so a
    process that reported 0 from start-up would page somebody about a source
    that was never configured.

    Pinned by calling the callbacks directly with the reader unset, not
    through a collection, for the reason M4 recorded for the queue gauges:
    the branch is unreachable through `register_push_gauges`, which assigns
    the reader *before* it creates the instruments, and the indirect version
    -- registering a reader that answers with an empty mapping -- passes
    against a guard that fabricates a zero.
    """
    monkeypatch.setattr("usher.telemetry._push_reader", None)
    assert list(_observe_push_connected(CallbackOptions())) == []
    assert list(_observe_push_reconnects(CallbackOptions())) == []
