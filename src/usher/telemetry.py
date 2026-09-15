"""Logging and tracing setup."""

import inspect
import logging
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from usher.config import Settings


def current_traceparent() -> str | None:
    """The active span as a W3C `traceparent`, or `None` outside a span.

    Carried on a job row so a worker's span can `Link` back to whatever
    enqueued the work — PRD 10's "why did the title I just opened take 45
    seconds" spans a request and a background execution minutes later, and
    nothing else joins them. A `Link` rather than a parent, because the
    request has usually already returned and a child span of a finished
    parent misstates causality.

    Returns `None` rather than a syntactically-valid all-zero traceparent
    when no span is active: the propagator declines to inject an invalid
    context, so an absent key is the SDK's own answer and not a special
    case invented here. A job enqueued outside a span therefore stores
    `NULL` and the worker starts an unlinked span, which is honest — the
    alternative is a link to a trace that never existed.
    """
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


#: The response header carrying the server span, spelled this way rather than
#: `X-Trace-Id`.
TRACERESPONSE_HEADER: Final = "traceresponse"


def traceresponse(span: trace.Span | None = None) -> str | None:
    """The active server span as a `traceresponse` value, or `None`."""
    span = trace.get_current_span() if span is None else span
    if not span.is_recording():
        return None
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return (
        f"00-{trace.format_trace_id(context.trace_id)}"
        f"-{trace.format_span_id(context.span_id)}"
        f"-{context.trace_flags:02x}"
    )


def inject_trace_context(record: Mapping[str, Any]) -> None:
    """Patch the active trace and span ids into every log record.

    A line in Loki then links to its trace and back again.

    Typed `Mapping[str, Any]` rather than `dict[str, Any]`: loguru's real
    `Record` (a `TypedDict`) satisfies `Mapping` but not the invariant `dict`,
    and mypy strict rejects the latter at the `configure()` call site below.
    """
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        record["extra"]["trace_id"] = format(context.trace_id, "032x")
        record["extra"]["span_id"] = format(context.span_id, "016x")


class _InterceptHandler(logging.Handler):
    """Redirects stdlib `logging` records into loguru.

    Without this, only code that calls `usher`'s own `logger` goes through
    the sink below: uvicorn's access/error logs, SQLAlchemy's warnings, and
    the OTel SDK's own exporter retry/failure messages (all stdlib
    `logging` users) print as unstructured plain text, ignore
    `settings.log_level`/`log_json`, and never get `trace_id`/`span_id`
    patched in — confirmed directly against a live run: every uvicorn
    access line printed as plain text (`INFO: 127.0.0.1 - "GET ..."`)
    alongside the JSON lines `usher`'s own logger produced. PRD 10 says
    "Every record is patched", not "every loguru record". Recipe is
    loguru's own documented one for this exact scenario, verbatim (see its
    README's "Entirely compatible with standard logging" section).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: Settings) -> None:
    logger.remove()
    logger.configure(patcher=inject_trace_context)
    logger.add(
        sys.stdout,
        level=settings.log_level,
        serialize=settings.log_json,
        backtrace=False,
        diagnose=False,
    )

    # uvicorn attaches handlers to its own loggers before `create_app()` runs,
    # and any other library may too. Clearing them and forcing `propagate=True`
    # is what makes redirecting the root logger below catch everything, instead
    # of printing each record twice.
    for name in list(logging.root.manager.loggerDict):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True
        stdlib_logger.disabled = False
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # **`httpx` logs one INFO line per request, and the redirect above is what
    # made it visible.** On the shipped defaults that is a ~900-character JSON
    # envelope per request on stdout, which is where every CLI command writes
    # the output an operator is reading.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_tracing(settings: Settings) -> None:
    """Install a real SDK `TracerProvider` and instrument SQLAlchemy and httpx.

    Unconditional and global, so any span started anywhere in the process gets
    a real trace and span id for `inject_trace_context` to correlate, whether
    or not there is anywhere to export it.
    """
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
        if settings.telemetry_enabled:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
            )
        trace.set_tracer_provider(provider)
    SQLAlchemyInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def configure_metrics(settings: Settings) -> None:
    """Install a real SDK `MeterProvider`, exporting only when telemetry is on.

    `configure_tracing`'s shape, for its reasons: a real if unexported provider
    binds every instrument to something real instead of the API's no-op
    default, and the `isinstance` guard stops repeated `create_app()` calls
    leaking a `PeriodicExportingMetricReader` export thread apiece.

    No metrics are registered here. Each is owned by the milestone that emits
    it; this is only the bootstrap they register against.
    """
    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        readers = (
            [PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=settings.otlp_endpoint))]
            if settings.telemetry_enabled
            else []
        )
        provider = MeterProvider(
            resource=Resource.create({"service.name": settings.service_name}),
            metric_readers=readers,
        )
        metrics.set_meter_provider(provider)


class _ReaderSlot[T]:
    """A replaceable reader behind an observable instrument.

    The SDK keeps only the first instrument registered under a name, so a second
    registration replaces the reader rather than the instrument. An unset reader
    observes nothing: on every series here a fabricated zero is indistinguishable
    from a real reading, and is the value an alert would act on.
    """

    def __init__(self) -> None:
        self._read: Callable[[], T] | None = None

    def set(self, read: Callable[[], T]) -> None:
        self._read = read

    def clear(self) -> None:
        self._read = None

    def observe(self, build: Callable[[T], Iterable[Observation]]) -> Iterable[Observation]:
        read = self._read
        return [] if read is None else list(build(read()))


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """One reading of the `jobs` table, by kind.

    Both maps are keyed by `JobKind.value` rather than by the enum: this
    module is imported by `services/` and `adapters/` alike and deliberately
    knows nothing about the domain, so a gauge label is a string here and
    the composition root is what turns an enum into one.
    """

    queued: Mapping[str, int] = field(default_factory=dict)
    parked: Mapping[str, int] = field(default_factory=dict)


QueueReader = Callable[[], QueueSnapshot]

_queue: _ReaderSlot[QueueSnapshot] = _ReaderSlot()


def register_queue_gauges(read: QueueReader) -> None:
    """PRD 10's `usher.jobs.queued` / `usher.jobs.parked`, by kind.

    `read` is synchronous and returns the caller's most recent full re-read of
    the `jobs` table, never a query: OTel invokes the callback from the metric
    reader's background thread, where awaiting asyncpg would deadlock. Safe to
    call repeatedly.
    """
    _queue.set(read)
    meter = metrics.get_meter("usher.jobs")
    meter.create_observable_gauge(
        "usher.jobs.queued",
        callbacks=[_observe_queued],
        unit="1",
        description="Jobs waiting to be claimed, by kind",
    )
    meter.create_observable_gauge(
        "usher.jobs.parked",
        callbacks=[_observe_parked],
        unit="1",
        description="Jobs parked with an error, by kind",
    )


def _observe_queued(options: CallbackOptions) -> Iterable[Observation]:
    return _queue.observe(lambda snapshot: _by_kind(snapshot.queued))


def _observe_parked(options: CallbackOptions) -> Iterable[Observation]:
    return _queue.observe(lambda snapshot: _by_kind(snapshot.parked))


def _by_kind(counts: Mapping[str, int]) -> Iterable[Observation]:
    return [Observation(count, {"kind": kind}) for kind, count in counts.items()]


@dataclass(frozen=True, slots=True)
class PushSnapshot:
    """One source's push lane, as PRD 10's two series see it.

    **`delivering`, not `connected`.** A series fed by the socket's *state*
    would be permanently green against the failure that actually happens -- a
    channel that upgraded, is held open, and delivers nothing.
    `usher.source.push.connected` keeps PRD 10's name, because a metric renamed
    is a dashboard panel silently blank, and reports the honest quantity.

    `reconnects` is cumulative for the lane rather than per connection,
    which is what `PushHealth` being one object across reconnects buys.
    """

    delivering: bool
    reconnects: int


PushReader = Callable[[], Mapping[str, PushSnapshot]]

_push: _ReaderSlot[Mapping[str, PushSnapshot]] = _ReaderSlot()


def register_push_gauges(read: PushReader) -> None:
    """PRD 10's `usher.source.push.connected` / `usher.source.push.reconnects`.

    `connected` is a gauge; `reconnects` is an asynchronous counter, because a
    cumulative total read out of a ledger is what that instrument is for.
    Reporting it as a gauge would put the wrong instrument type on the wire
    under a documented name.
    """
    _push.set(read)
    meter = metrics.get_meter("usher.push")
    meter.create_observable_gauge(
        "usher.source.push.connected",
        callbacks=[_observe_push_connected],
        unit="1",
        description="1 when a source's push channel is delivering messages, 0 otherwise",
    )
    meter.create_observable_counter(
        "usher.source.push.reconnects",
        callbacks=[_observe_push_reconnects],
        unit="1",
        description="Cumulative push reconnects for a source's lane",
    )


def _observe_push_connected(options: CallbackOptions) -> Iterable[Observation]:
    return _push.observe(lambda lanes: _by_source(lanes, lambda one: 1 if one.delivering else 0))


def _observe_push_reconnects(options: CallbackOptions) -> Iterable[Observation]:
    return _push.observe(lambda lanes: _by_source(lanes, lambda one: one.reconnects))


def _by_source(
    lanes: Mapping[str, PushSnapshot], select: Callable[[PushSnapshot], int]
) -> Iterable[Observation]:
    return [Observation(select(one), {"source": source}) for source, one in lanes.items()]


SseReader = Callable[[], int]

_sse: _ReaderSlot[int] = _ReaderSlot()


def register_sse_gauge(read: SseReader) -> None:
    """PRD 10's `usher.sse.connections`.

    The one live read among these: `len()` on an in-memory set is safe to call
    from the metric reader's background thread.
    """
    _sse.set(read)
    metrics.get_meter("usher.api").create_observable_gauge(
        "usher.sse.connections",
        callbacks=[_observe_sse_connections],
        unit="1",
        description="Open SSE client connections",
    )


def _observe_sse_connections(options: CallbackOptions) -> Iterable[Observation]:
    return _sse.observe(lambda open_connections: [Observation(open_connections)])


SchedulerReader = Callable[[], Mapping[str, float]]

_scheduler: _ReaderSlot[Mapping[str, float]] = _ReaderSlot()


def register_scheduler_gauges(read: SchedulerReader) -> None:
    """PRD 10's `usher.scheduler.job.due`, per job.

    Seconds since a job's `last_done()` minus its period; negative means not yet
    due. A job with no reading has no entry rather than a zero, which would read
    as "exactly due".
    """
    _scheduler.set(read)
    metrics.get_meter("usher.scheduler").create_observable_gauge(
        "usher.scheduler.job.due",
        callbacks=[_observe_job_due],
        unit="s",
        description="Seconds a scheduled job is overdue; negative means not yet due",
    )


def _observe_job_due(options: CallbackOptions) -> Iterable[Observation]:
    return _scheduler.observe(
        lambda readings: [Observation(due, {"job": job}) for job, due in readings.items()]
    )


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    """One reading of the embedding backlog."""

    stale: int = 0
    refused: int = 0
    # **A third number, and it is about a different table.** `stale`/`refused` are
    # `title_embeddings`; this is `title_neighbors` rows whose `blend_fingerprint` is
    # not the running one -- M7's fourth similarity signal changed what every stored
    # score *means*, and before that column existed nothing could tell a row computed
    # under the old blend from one computed under the new.
    neighbors_stale: int = 0


SearchReader = Callable[[], SearchSnapshot]

_search: _ReaderSlot[SearchSnapshot] = _ReaderSlot()


def register_search_gauges(read: SearchReader) -> None:
    """PRD 10's embedding-staleness gauges, and `usher.similarity.neighbors.stale`.

    `read` returns the caller's most recent full re-read, never a query, for
    `register_queue_gauges`' reason. The third instrument takes a different
    meter because `usher index --backfill` does not drain it.
    """
    _search.set(read)
    meter = metrics.get_meter("usher.search")
    meter.create_observable_gauge(
        "usher.search.embeddings.stale",
        callbacks=[_observe_embeddings_stale],
        unit="1",
        description="Titles in the embedded population whose vector is missing or out of date",
    )
    meter.create_observable_gauge(
        "usher.search.embeddings.refused",
        callbacks=[_observe_embeddings_refused],
        unit="1",
        description="Titles whose composed document was degenerate, so no vector was written",
    )
    # A different meter name, because this is `title_neighbors` rather than the
    # embedding backlog, and a dashboard grouping the two under one subsystem
    # would suggest one `usher index --backfill` drains both. It does not:
    # this one is drained by `usher similar --rebuild`, which nothing
    # schedules.
    metrics.get_meter("usher.similarity").create_observable_gauge(
        "usher.similarity.neighbors.stale",
        callbacks=[_observe_neighbors_stale],
        unit="1",
        description="Stored neighbour rows computed under a different similarity blend",
    )


def _observe_embeddings_stale(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.stale)])


def _observe_embeddings_refused(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.refused)])


def _observe_neighbors_stale(options: CallbackOptions) -> Iterable[Observation]:
    return _search.observe(lambda snapshot: [Observation(snapshot.neighbors_stale)])


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
    configure_metrics(settings)


# --------------------------------------------------------------------------- The shared
# cache counters.
_cache_meter = metrics.get_meter("usher.cache")

CACHE_HITS = _cache_meter.create_counter(
    "usher.cache.hits", description="Cache reads that found a live entry"
)
CACHE_MISSES = _cache_meter.create_counter(
    "usher.cache.misses",
    description="Cache reads that found nothing or an expired entry",
)
