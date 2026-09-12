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

**The composer's `propose` span is the one subject here that M4 did not
build.** It lives with the rest of PRD 10's span tree rather than with the
composer's own cases because the claim is the document's -- one span per
*registered* provider, two attributes, and a parent that differs by lane --
and because both of its roots are reachable with no database at all.
"""

import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, TraceFlags
from pydantic import SecretStr

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.job_scope import worker_over
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.metadata_provider import _MOVIE_PAYLOAD, FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import Library
from usher.adapters.tmdb import TmdbClient
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import Job, JobKind, JobPriority
from usher.domain.rows import RowCard
from usher.domain.title import Title
from usher.ports.errors import PortUnavailable
from usher.ports.ingest import ProviderRef
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.enrich import EnrichService
from usher.services.home import HomeService
from usher.services.ingest import IngestService
from usher.services.jobs import _links_for
from usher.services.matching import MatchService
from usher.services.visibility import VisibilityService
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
    ran: list[Job] = []

    async def handler(job: Job) -> None:
        ran.append(job)

    worker = worker_over(queue, {JobKind.ENRICH: handler}, commit=_no_commit)
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
        queue=FakeJobQueue(),
    )
    await service.enrich(title.id)
    recorded = _recorded(meter_reader)
    assert "usher.enrichment.latency" in recorded
    assert recorded["usher.enrichment.latency"][0][0]["outcome"] == "enriched"


async def test_a_demand_enrichment_and_a_background_one_are_two_series_on_the_latency_histogram(
    meter_reader: InMemoryMetricReader,
) -> None:
    """🔴 D12's headline: `usher.enrichment.latency` has to carry `trigger`.

    PRD 10's alert table asks for *"Demand-triggered p99 > 5 s for 15 min"*, and
    at M4 the series carried `outcome` alone -- so the alert named a dimension
    the shipped series did not have and could not be written at all. The
    withholding was deliberate and **conditional**: *"nothing in M4 enriches on
    demand (`JobPriority.DEMAND` is defined and unused until M5), so a `trigger`
    label would carry one constant value"*. M5 spent that condition --
    `services/titles.py` and three routers now enqueue `enrich` at
    `JobPriority.DEMAND` -- so the label is a real series and the alert is
    expressible.

    **Two drives, not one, and that is the positive control.** A case that
    recorded a single point and asserted `"trigger" in attributes` would pass
    against a hard-coded constant, which is exactly the state M4 declined to
    ship. Two points whose attribute sets *differ on `trigger`* and *agree on
    `outcome`* is the claim that cannot be satisfied by a constant, and it is
    also the claim the alert's `{trigger="demand"}` selector depends on.

    ⚠️ **OTel aggregates on the attribute *set*.** Swapping the two keys in the
    record call's dict literal produces an identical stream, so this case
    deliberately asserts on values by key rather than on any ordering.
    """
    titles = FakeTitleRepository()
    provider = FakeMetadataProvider()
    made: list[Title] = []
    for name, tmdb_id in (("Fight Club", 90000550), ("Se7en", 90000807)):
        title = Title(
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=name,
            year=1999,
            tmdb_id=tmdb_id,
            enrichment_state=EnrichmentState.STUB,
        )
        await titles.add(title)
        made.append(title)
        # Two *titles*, so the two drives are two independent passes through
        # `_apply` rather than one title enriched twice -- a second enrichment
        # of the same row takes the cache-age branch and records nothing.
        provider.seed(
            ProviderRef(provider="tmdb", value=str(tmdb_id), kind=TitleKind.MOVIE),
            {**_MOVIE_PAYLOAD, "id": tmdb_id, "title": name},
        )
    service = EnrichService(
        titles=titles,
        episodes=FakeEpisodeRepository(),
        payloads=FakeRawPayloadStore(),
        provider=provider,
        commit=_no_commit,
        events=FakeEventPublisher(),
        queue=FakeJobQueue(),
    )
    await service.enrich(made[0].id, priority=JobPriority.DEMAND)
    await service.enrich(made[1].id, priority=JobPriority.BACKFILL)

    points = _recorded(meter_reader).get("usher.enrichment.latency", [])
    assert points, "no latency points recorded"
    assert len(points) == 2, (
        "the two enrichments collapsed into one series, so `trigger` is not on the "
        f"attribute set OTel aggregates by: {[attributes for attributes, _ in points]}"
    )

    by_trigger = {str(attributes.get("trigger")): attributes for attributes, _ in points}
    assert set(by_trigger) == {"demand", "background"}, (
        "PRD 10's vocabulary for this label is `demand` | `background`, the same two "
        f"values its span attributes use; this recorded {sorted(by_trigger)}"
    )
    assert {str(attributes["outcome"]) for attributes in by_trigger.values()} == {"enriched"}, (
        "the two drives disagree on `outcome`, so the difference this case measures is "
        "not the one it names"
    )


async def test_the_rung_the_visible_lane_promotes_at_is_recorded_as_a_demand_enrichment(
    meter_reader: InMemoryMetricReader,
) -> None:
    """🔴 The boundary the `trigger` label is drawn on, exercised at the rung
    the largest population actually arrives at.

    The case above drives `DEMAND` (100) and `BACKFILL` (20), which classify
    the same whether `_trigger_for` is spelled `>=` or `>`. `VISIBLE` (80) is
    the threshold itself and no case reached it, so `priority >= VISIBLE` →
    `priority > VISIBLE` was a behavioural mutation the whole suite missed --
    and it is not a small one. `services/visibility.py` enqueues **every
    unfinished title on every browse, search, suggest and home response** at
    `VISIBLE`; `services/titles.py` and three routers enqueue at `DEMAND` one
    title at a time. So `>` moves the plural lane -- the larger population by
    orders of magnitude -- onto `background`, and `trigger` is the label
    `dashboards/alerts/usher.yml`'s *"Enrichment SLA missed"* scopes on with
    `{trigger="demand"}`. The alert keeps firing, keeps looking healthy, and
    silently measures single-title opens only.

    **The rung is read out of the queue rather than written down here.** A
    case asserting `_trigger_for(JobPriority.VISIBLE) == "demand"` pins a
    constant against itself: if the promotion lane moved to `NEW` tomorrow the
    case would still pass and still say nothing about the shipped population.
    Taking the priority off the `JobRequest` the visibility service actually
    enqueued makes the two halves one claim -- *the rung a screen promotes at
    is the rung the histogram calls demand* -- which is the claim the alert
    depends on.
    """
    queue = FakeJobQueue()
    titles = FakeTitleRepository()
    title = Title(
        kind=TitleKind.MOVIE,
        name="Zodiac",
        sort_name="Zodiac",
        year=2007,
        tmdb_id=90001949,
        # A skeleton, which is what a browse page is mostly made of: 1,139,982
        # of 1,273,313 titles when `services/visibility.py` was written.
        enrichment_state=EnrichmentState.SKELETON,
    )
    await titles.add(title)

    promoted = await VisibilityService(queue=queue, titles=titles).seen([title])

    assert promoted == 1, "the visibility lane promoted nothing, so there is no rung to read"
    enqueued = queue.jobs_of(JobKind.ENRICH)
    assert len(enqueued) == 1, f"expected one enrich job off one unfinished title: {enqueued}"
    rung = enqueued[0].priority
    assert rung == JobPriority.VISIBLE, (
        f"the screen lane enqueues at {rung}, not `VISIBLE` -- this case's premise has "
        "moved and the label boundary it measures is no longer the one that matters"
    )

    provider = FakeMetadataProvider()
    provider.seed(
        ProviderRef(provider="tmdb", value=str(title.tmdb_id), kind=TitleKind.MOVIE),
        {**_MOVIE_PAYLOAD, "id": title.tmdb_id, "title": title.name},
    )
    service = EnrichService(
        titles=titles,
        episodes=FakeEpisodeRepository(),
        payloads=FakeRawPayloadStore(),
        provider=provider,
        commit=_no_commit,
        events=FakeEventPublisher(),
        queue=FakeJobQueue(),
    )
    await service.enrich(title.id, priority=rung)

    points = _recorded(meter_reader).get("usher.enrichment.latency", [])
    assert len(points) == 1, f"expected one latency point off one enrichment: {points}"
    attributes = points[0][0]
    assert str(attributes.get("trigger")) == "demand", (
        f"an enrichment at the rung a screen promotes at was recorded as "
        f"{attributes.get('trigger')!r}; the boundary is `>=` and not `>`, and every "
        "browse-driven enrichment has just left the population "
        '`{trigger="demand"}` selects'
    )


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
    monkeypatch.setattr("usher.telemetry._queue._read", None)
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


# The composer's two lanes, and the registry both of them walk. One provider
# proposes a row and the other proposes nothing, because "one span per
# *registered* provider" and "one span per provider that returned something"
# are the same assertion against a registry where every provider fires.
_PROPOSING = "recently-added"
_SILENT = "next-up"
# A span the proposing provider opens inside `propose`, so that "the composer
# made this span current" is observable and not only "the composer gave this
# span a parent". See `_TracingRowProvider`.
_INSIDE = "provider.work"


def _card() -> RowCard:
    return RowCard(
        title_id=new_id(),
        kind=TitleKind.MOVIE,
        name="An Invented Title",
        enrichment_state=EnrichmentState.SKELETON,
    )


class _TracingRowProvider(FakeRowProvider):
    """A `FakeRowProvider` that opens a span of its own inside `propose`.

    **This is the arm `start_span` fails, and the parent-of-`propose` arm is
    not it.** Measured rather than assumed, on 2026-09-07 with this class not
    yet written: planting `start_as_current_span` -> `start_span` and scoring
    it over `tests/unit` plus all three span-observing integration files
    killed nothing (4,811 passed, 4 skipped). `Tracer.start_span` with no
    explicit `context` still parents to the *current* span, so a `propose`
    minted that way is still `home.compose`'s child and every assertion about
    its own parent stays green.

    What it does break is the half `_build`'s docstring gives as its *reason*
    rather than as its mechanism: `start_span` never makes the span current,
    so the work done while proposing -- for a real provider, every SQLAlchemy
    statement span it issues -- reparents up to the composition. That is the
    difference between a trace that answers "what did this request do" and one
    that answers "what happened around then", and only a span emitted *inside*
    `propose` can see it. (`_build`'s "rather than a second root" is the same
    over-claim one phase later, and `row.build` has no arm for it either --
    left alone here because D2 owns `_compose` and nothing else.)
    """

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        with trace.get_tracer("test").start_as_current_span(_INSIDE):
            return await super().propose(ctx)


def _registry() -> list[RowProvider]:
    return [
        _TracingRowProvider(
            proposals=(ScoredRow(row=FakeRow(_PROPOSING, cards=(_card(),)), score=0.9),),
            slug_prefix=_PROPOSING,
        ),
        FakeRowProvider(proposals=(), slug_prefix=_SILENT),
    ]


def _attributes(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def _named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


async def test_every_provider_that_proposes_opens_a_propose_span_under_the_composition(
    span_exporter: InMemorySpanExporter,
) -> None:
    """PRD 10's `propose`, and the four properties that make it worth having.

    Proposal ran inside `home.compose` and was untraced individually until
    M10, so a provider slow to *propose* and cheap to *build* was visible
    only in the parent's duration -- and at the scale ceiling that is 302.9
    ms of a 710.3 ms p50 for `next-up` alone (ADR-0025, re-measured there
    against 1,277,878 owned items; the 23.9 ms figure belongs to a
    5,200-copy household and means nothing here).

    **One span per *registered* provider**, not one per provider that
    returned something: `ProviderReport` keeps a line for the silent ones
    for the same reason, since an absent provider and a silent one are the
    two states the breakdown exists to tell apart. **`usher.row.provider` is
    the `slug_prefix`**, the same attribute `row.build` carries, so one
    group-by spans both phases. And **parentage, not existence** -- a
    composer emitting `propose` as a second root has valid ids, exports
    traces, and carries the name the document asks for.

    The fourth is **currentness**, which parentage does not imply and which
    only a span opened *inside* `propose` can see: see `_TracingRowProvider`
    for the plant that proved the difference.
    """
    providers = _registry()

    await HomeService(providers=providers).compose(Library().context())

    exported = span_exporter.get_finished_spans()
    # The positive control. An exporter that collected nothing satisfies
    # every per-span assertion below vacuously.
    assert exported, "the exporter collected no spans"
    compositions = _named(exported, "home.compose")
    assert len(compositions) == 1, [span.name for span in exported]
    assert compositions[0].context is not None
    proposals = _named(exported, "propose")
    assert len(proposals) == len(providers), (
        f"{len(proposals)} propose spans over a registry of {len(providers)}"
    )
    by_provider = {_attributes(span)["usher.row.provider"]: span for span in proposals}
    assert set(by_provider) == {_PROPOSING, _SILENT}
    assert _attributes(by_provider[_PROPOSING])["usher.row.proposed"] == 1
    assert _attributes(by_provider[_SILENT])["usher.row.proposed"] == 0
    for span in proposals:
        assert span.parent is not None, "a propose span is a second root"
        assert span.parent.span_id == compositions[0].context.span_id
    # And **current**, not merely parented: the span the provider opened while
    # proposing hangs off its own `propose`. `start_span` satisfies every
    # assertion above and fails here, which is why the arm exists.
    proposing = by_provider[_PROPOSING]
    assert proposing.context is not None
    inside = _named(exported, _INSIDE)
    assert len(inside) == 1, [span.name for span in exported]
    assert inside[0].parent is not None, "the provider's own span is a second root"
    assert inside[0].parent.span_id == proposing.context.span_id


async def test_a_refresh_opens_propose_spans_with_no_home_compose_parent(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The mirror PRD 10 already draws for `row.build`, one phase earlier.

    `rebuild` is `_compose` without the cache read and **without a
    `home.compose` of its own**, so the serve-stale lane's spans hang off
    `rows.refresh` instead -- deliberately, since minting a second
    `home.compose` would double the count every dashboard reads as
    "requests that composed". This fails if somebody "fixes" the refresh by
    minting one.

    The rebuild runs inside a span here for the reason
    `test_the_refresh_is_a_root_span_linked_to_the_request_that_served_stale`
    gives: without an ambient parent to be wrongly attached to, "no
    `home.compose` ancestor" is what an unparented span produces anyway and
    the assertion could not fail.
    """
    providers = _registry()

    with trace.get_tracer("test").start_as_current_span("rows.refresh") as refresh:
        await HomeService(providers=providers).rebuild(Library().context())
        refresh_id = refresh.get_span_context().span_id

    exported = span_exporter.get_finished_spans()
    assert exported, "the exporter collected no spans"
    assert not _named(exported, "home.compose"), (
        "a refresh minted a home.compose -- PRD 10 counts those as compositions a request paid for"
    )
    proposals = _named(exported, "propose")
    assert len(proposals) == len(providers), (
        f"{len(proposals)} propose spans over a registry of {len(providers)}"
    )
    for span in proposals:
        assert span.parent is not None, "a propose span is a second root"
        assert span.parent.span_id == refresh_id


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
