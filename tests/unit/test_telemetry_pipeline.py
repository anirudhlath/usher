"""PRD 10's metric catalogue and span tree, for the pipeline M4 built.

**A metric that is documented and never emitted is a dashboard panel that
is permanently empty, and nothing distinguishes that from a healthy zero.**
So every case below drives the code that owns the instrument and reads the
value back out of an `InMemoryMetricReader` -- asserting the instrument
*exists* would pass against a `create_histogram` nobody ever calls, which
is precisely the failure mode ("replace `.record(...)` with `pass`") the
mutation sweep looks for.

The span cases are here rather than in `tests/integration/` because they
need no database. What they cannot say is whether a pipeline span nests
under a *request*; that is `tests/integration/test_pipeline_spans.py`, and
it needs a real app.
"""

import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, TraceFlags
from pydantic import SecretStr

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.adapters.tmdb import TmdbClient
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import Job, JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.enrich import EnrichService
from usher.services.ingest import IngestService
from usher.services.jobs import JobWorker, _links_for
from usher.services.matching import MatchService
from usher.telemetry import (
    QueueSnapshot,
    _observe_parked,
    _observe_queued,
    register_queue_gauges,
)

# Every metric M4 owes, from PRD 10's table. Named here rather than
# discovered from the code, so a rename in `src/` fails this file instead of
# quietly moving the dashboard's target.
PRD_10_M4_METRICS = frozenset(
    {
        "usher.ingest.items",
        "usher.match.result",
        "usher.jobs.duration",
        "usher.jobs.queued",
        "usher.jobs.parked",
        "usher.enrichment.latency",
        "usher.provider.requests",
    }
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


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


def _movie(external_id: str, tmdb_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Movie {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        provider_ids={"tmdb": tmdb_id},
    )


def _ingest_service() -> IngestService:
    titles = FakeTitleRepository()
    matching = FakeTitleMatchRepository(titles)
    queue = FakeJobQueue()
    return IngestService(
        matcher=MatchService(titles=titles, matching=matching, queue=queue),
        matching=matching,
        media_items=FakeMediaItemRepository(),
        episodes=FakeEpisodeRepository(),
        queue=queue,
    )


async def test_a_walk_records_ingest_items_and_match_result(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10's `usher.ingest.items` (source, result) and
    `usher.match.result` (method, confident). Both are counters a dashboard
    integrates, so a `pass` in place of either leaves "library growth per
    week" (dashboard 1) a flat line at zero."""
    await _ingest_service().ingest_batch(
        new_id(),
        [_movie("m1", "90000550"), _movie("m2", "90000551")],
        observed_at=datetime.now(UTC),
    )
    recorded = _recorded(meter_reader)
    assert "usher.ingest.items" in recorded
    assert sum(value for _, value in recorded["usher.ingest.items"]) == 2
    assert {"source", "result"} <= set(recorded["usher.ingest.items"][0][0])
    assert "usher.match.result" in recorded
    assert {"method", "confident"} <= set(recorded["usher.match.result"][0][0])


async def test_a_job_records_its_duration_by_kind(meter_reader: InMemoryMetricReader) -> None:
    """PRD 10's `usher.jobs.duration` (kind). Dashboard 3 plots enrichment
    p50/p99 off it and the "enrichment SLA missed" alert reads the same
    series."""
    queue = FakeJobQueue()
    worker = JobWorker(queue, _no_commit)
    ran: list[Job] = []

    async def handler(job: Job) -> None:
        ran.append(job)

    worker.register(JobKind.ENRICH, handler)
    from usher.ports.jobs import JobRequest

    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(new_id()), priority=JobPriority.NEW)]
    )
    assert await worker.run_once() == 1
    recorded = _recorded(meter_reader)
    assert "usher.jobs.duration" in recorded
    assert recorded["usher.jobs.duration"][0][0]["kind"] == "enrich"


async def test_enrichment_records_prd_10s_latency_metric(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`usher.enrichment.latency`, PRD 10's own name. This was emitted as
    `usher.enrich.duration` until M4 Task 24 -- a near-miss name is a
    permanently empty panel, and nothing tells it apart from a healthy
    zero."""
    titles = FakeTitleRepository()
    title = Title(
        kind=TitleKind.MOVIE,
        name="Fight Club",
        sort_name="Fight Club",
        year=1999,
        tmdb_id=90000550,
        enrichment_state=EnrichmentState.STUB,
    )
    await titles.add(title)
    service = EnrichService(
        titles=titles,
        episodes=FakeEpisodeRepository(),
        payloads=FakeRawPayloadStore(),
        provider=FakeMetadataProvider(),
        commit=_no_commit,
        events=FakeEventPublisher(),
    )
    await service.enrich(title.id)
    recorded = _recorded(meter_reader)
    assert "usher.enrichment.latency" in recorded
    assert recorded["usher.enrichment.latency"][0][0]["outcome"] == "enriched"


async def test_a_provider_request_is_counted_by_status(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10's `usher.provider.requests` (provider, status). Dashboard 3
    wants "TMDb requests/sec against the ~40 ceiling with 429 count", which
    is a counter rate rather than a histogram's sampled `_count`."""
    client = TmdbClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 1}))
        ),
        SecretStr("key"),
        requests_per_second=1000.0,
    )
    await client.get("/movie/90000550")
    recorded = _recorded(meter_reader)
    assert "usher.provider.requests" in recorded
    attributes, value = recorded["usher.provider.requests"][0]
    assert attributes == {"provider": "tmdb", "status": "200"}
    assert value == 1


async def test_a_provider_request_that_never_answered_is_still_counted(
    meter_reader: InMemoryMetricReader,
) -> None:
    """A transport failure reaches no status line at all. Counting only the
    answered half makes the "provider degraded" alert's denominator drop
    exactly when the upstream is worst, so the rate reads *low* during an
    outage."""

    def _explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = TmdbClient(
        httpx.AsyncClient(transport=httpx.MockTransport(_explode)),
        SecretStr("key"),
        requests_per_second=1000.0,
    )
    with pytest.raises(PortUnavailable):
        await client.get("/movie/90000550")
    recorded = _recorded(meter_reader)
    assert recorded["usher.provider.requests"][0][0]["status"] == "error"


def test_the_provider_metric_names_this_provider() -> None:
    """The counter's `provider` label is a literal in `client.py` because
    `provider.py` imports that module and reaching back would be a cycle.
    That makes it a string that can drift from `PROVIDER_NAME` with nothing
    to notice, so the two are pinned together here."""
    from usher.adapters.tmdb.provider import PROVIDER_NAME

    assert PROVIDER_NAME == "tmdb"


def test_the_queue_gauges_report_what_the_last_read_found(
    meter_reader: InMemoryMetricReader,
) -> None:
    """PRD 10's `usher.jobs.queued` / `usher.jobs.parked`. Observable, so
    the value is pulled at collection time rather than pushed -- which is
    what makes them survive a `complete` that deletes a row without anything
    decrementing a counter."""
    register_queue_gauges(
        lambda: QueueSnapshot(queued={"enrich": 3, "match": 0}, parked={"enrich": 1})
    )
    recorded = _recorded(meter_reader)
    assert dict(
        (str(attributes["kind"]), value) for attributes, value in recorded["usher.jobs.queued"]
    ) == {"enrich": 3, "match": 0}
    assert recorded["usher.jobs.parked"] == [({"kind": "enrich"}, 1)]


def test_the_queue_gauges_report_nothing_before_anything_has_read_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fabricated zero is the one value that makes PRD 10's "ingest
    stalled" alert quietly wrong: it fires on depth *rising*, and a gauge
    that reported 0 from process start until the first read would show a
    step up that no queue actually took.

    Pinned by calling the callback directly with the reader unset, not
    through a collection, and for the same reason `_links_for`'s validity
    guard is pinned directly: the branch is unreachable through
    `register_queue_gauges`, which assigns the reader *before* it creates
    the instruments, so no collection can ever observe the `None`. Mutation
    showed the indirect version -- registering a reader that answers with an
    empty `QueueSnapshot` -- passes against a guard that fabricates a zero,
    because an empty snapshot reports nothing either way.
    """
    monkeypatch.setattr("usher.telemetry._queue_reader", None)
    assert list(_observe_queued(CallbackOptions())) == []
    assert list(_observe_parked(CallbackOptions())) == []


def test_re_registering_the_gauges_replaces_the_reader(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The SDK keeps only the *first* observable gauge registered under a
    name and silently discards the rest -- verified directly. So a second
    `register_queue_gauges` that created a second instrument would leave the
    first, dead reader reporting forever. The reader is swapped instead."""
    register_queue_gauges(lambda: QueueSnapshot(queued={"enrich": 1}))
    _recorded(meter_reader)
    register_queue_gauges(lambda: QueueSnapshot(queued={"enrich": 9}))
    recorded = _recorded(meter_reader)
    assert recorded["usher.jobs.queued"] == [({"kind": "enrich"}, 9)]


async def test_the_pipeline_span_names_match_prd_10s_tree(
    span_exporter: InMemorySpanExporter,
) -> None:
    """PRD 10 draws `ingest.item -> match.title`. A tree whose names drifted
    makes every Tempo query in the shipped dashboards wrong, silently."""
    await _ingest_service().ingest_batch(
        new_id(), [_movie("m1", "90000550")], observed_at=datetime.now(UTC)
    )
    names = {span.name for span in span_exporter.get_finished_spans()}
    assert {"ingest.item", "match.title"} <= names


async def test_match_title_is_a_child_of_ingest_item(
    span_exporter: InMemorySpanExporter,
) -> None:
    """PRD 10 draws them nested, and a flat pair of siblings answers "why
    was this batch slow" with two unrelated durations."""
    await _ingest_service().ingest_batch(
        new_id(), [_movie("m1", "90000550")], observed_at=datetime.now(UTC)
    )
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    ingest, match = spans["ingest.item"], spans["match.title"]
    assert ingest.context is not None and match.parent is not None
    assert match.parent.span_id == ingest.context.span_id


def test_a_worker_span_links_rather_than_parents(span_exporter: InMemorySpanExporter) -> None:
    """A job's span is a root with a `Link`, never a child: the request that
    enqueued it has usually already returned, and growing a branch on a
    finished trace misstates causality."""
    job = Job(
        kind=JobKind.ENRICH,
        key=str(new_id()),
        priority=JobPriority.NEW,
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    links = _links_for(job)
    assert len(links) == 1
    assert format(links[0].context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_an_invalid_traceparent_produces_no_link_at_all() -> None:
    """Pinned directly rather than through a span, because the OTel SDK
    *also* silently drops an invalid `Link` on the way in -- so a worker
    that built one records the same empty `links` tuple a worker that
    refused to would. The guard is unobservable through the span it guards
    and survives every indirect assertion."""
    job = Job(
        kind=JobKind.ENRICH,
        key=str(new_id()),
        priority=JobPriority.NEW,
        traceparent="00-00000000000000000000000000000000-0000000000000000-01",
    )
    assert _links_for(job) == []
    # And the reason the direct pin is needed, demonstrated: an invalid link
    # handed to a real span is dropped by the SDK, so the span cannot tell
    # the two implementations apart.
    invalid = Link(SpanContext(0, 0, is_remote=True, trace_flags=TraceFlags(1)))
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe", links=[invalid]) as span:
        assert getattr(span, "links", ()) == ()


def _instrument_names(reader: InMemoryMetricReader) -> set[str]:
    """Every instrument name this process has created, from two places.

    Module-level instruments (`_meter.create_counter(...)` at import) are
    reachable by walking `usher.*` for `_ProxyInstrument`s, which keep their
    name whether or not a real provider has resolved them yet. The queue
    gauges are not module-level -- `register_queue_gauges` creates them --
    so they come from the reader instead, after registering a reader that
    reports one series per gauge.
    """
    names = {
        instrument._name
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("usher")
        for instrument in vars(module).values()
        if isinstance(instrument, _ProxyInstrument)
    }
    register_queue_gauges(lambda: QueueSnapshot(queued={"enrich": 0}, parked={"enrich": 0}))
    return names | set(_recorded(reader))


def test_every_prd_10_metric_m4_owes_actually_exists(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The catalogue as a set, read off the instruments themselves rather
    than restated. Each name has its own case above that drives the code
    emitting it -- this is the one that fails when a rename in `src/` moves
    a dashboard's target, even if whoever renamed it also updated the case
    that drives it.
    """
    assert _instrument_names(meter_reader) >= PRD_10_M4_METRICS


async def _no_commit() -> None:
    return None
