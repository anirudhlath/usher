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

# Every metric PRD 10 marks M5 and this group owes. Named here rather than
# discovered from the code, so a rename in `src/` fails this file instead of
# quietly moving a dashboard's target.
PRD_10_M5_PUSH_METRICS = frozenset({"usher.source.push.events"})

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


def _instrument_names() -> set[str]:
    """Every module-level instrument name this process has created.

    Reachable by walking `usher.*` for `_ProxyInstrument`s, which keep their
    name whether or not a real provider has resolved them yet -- the same
    trick `tests/unit/test_telemetry_pipeline.py` uses for M4's catalogue.
    """
    return {
        instrument._name
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("usher")
        for instrument in vars(module).values()
        if isinstance(instrument, _ProxyInstrument)
    }


def test_every_prd_10_push_metric_actually_exists() -> None:
    """The catalogue as a set, read off the instruments themselves rather
    than restated. Each name has its own case above that drives the code
    emitting it -- this is the one that fails when a rename in `src/` moves
    a dashboard's target, even if whoever renamed it also updated the case
    that drives it. PRD 10 already prices the failure: a metric emitted
    under a near-miss name is a permanently empty panel that nothing
    distinguishes from a healthy zero.
    """
    assert _instrument_names() >= PRD_10_M5_PUSH_METRICS


def test_the_module_owning_those_instruments_is_imported() -> None:
    """`_instrument_names` walks `sys.modules`, so a catalogue case whose
    module was never imported compares an empty set against a set it happens
    to contain and passes having measured nothing. Pinned rather than relied
    on -- the same family as "a harness must refuse to classify a run that
    did not run"."""
    assert "usher.services.push" in sys.modules
