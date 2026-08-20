"""Logging and tracing setup.

Telemetry is optional: with no OTLP endpoint configured no exporter object
is constructed at all and Usher runs normally. See PRD 10 and ADR-0007.
"""

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


#: The response header carrying the server span, and the reason it is spelled
#: this way rather than `X-Trace-Id`.
#:
#: **`traceresponse` is a *withdrawn draft* name that the OpenTelemetry
#: ecosystem shipped anyway, and reading the spec is what says so.** Checked
#: 2026-08-19 against three documents rather than from memory:
#:
#: * `https://www.w3.org/TR/trace-context-2/` — the published Trace Context
#:   Level 2 Candidate Recommendation — defines `traceparent` and `tracestate`
#:   and **no response header at all**. The string `traceresponse` does not
#:   occur in it.
#: * `w3c/trace-context`'s `spec/21-http_response_header_format.md` on `main`
#:   is now titled *Trace Context Server Timing Metric Format*: the response
#:   binding moved onto `Server-Timing` under the metric name `trace`, e.g.
#:   `server-timing: trace;desc=00-<trace-id>-<span-id>-<flags>`.
#: * The **value grammar did not move with it**, and that is the half that
#:   matters here. Verbatim from that file:
#:
#:       value            = version "-" version-format
#:       version          = 2HEXDIGLC   ; version ff is forbidden
#:       version-format   = trace-id "-" child-id "-" trace-flags
#:       trace-id         = 32HEXDIGLC  ; All zeroes forbidden
#:       child-id         = 16HEXDIGLC  ; All zeroes forbidden
#:       trace-flags      = 2HEXDIGLC
#:
#: So the bytes below are exactly what the current spec specifies; only the
#: field they are carried in is contested. `traceresponse` is kept because it
#: is what `opentelemetry.instrumentation.propagators.TraceResponsePropagator`
#: emits — i.e. what *this* stack already speaks — and because a
#: `Server-Timing` entry is a shared, comma-joined namespace a proxy also
#: writes to. Adding the `Server-Timing` spelling later is one more `setter.set`
#: over the same value; nothing here would change.
TRACERESPONSE_HEADER: Final = "traceresponse"


def traceresponse(span: trace.Span | None = None) -> str | None:
    """The active server span as a `traceresponse` value, or `None`.

    `None` — meaning **no header at all** — in exactly two situations, and the
    distinction is the same one `current_traceparent` above draws and the same
    one `_observations` draws for a gauge that has no reader:

    * **the span is not recording.** A dropped span's id names a trace that was
      never exported, so a link built from it opens an empty Tempo page. "This
      response has no trace" and "this response has a trace you cannot find"
      are different facts and only the first one is honest.
    * **the context is invalid** — `INVALID_SPAN`'s all-zero trace id, which is
      what `get_current_span()` answers outside any span. A header reading
      `00-000…0-000…0-00` is syntactically well-formed, passes every regex, and
      is a lie; the spec forbids it in as many words (*"All zeroes forbidden"*,
      for both ids).

    Takes the span rather than always reading the ambient one so the format is
    testable against a span a test constructed, which is what separates "the
    header matches a real span id" from "the header matches a regex" — a
    hard-coded constant satisfies the second.
    """
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
    """Patch the active trace and span ids into every log record, so a line
    in Loki links to its trace and back again.

    Typed `Mapping[str, Any]` rather than `dict[str, Any]`: loguru's real
    `Record` (a `TypedDict`) satisfies `Mapping` but not the invariant
    `dict`, and mypy strict rejects the latter at the `configure()` call
    site below (confirmed directly; see commit history for the fence this
    replaced).
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

    # uvicorn attaches its own handlers directly to the "uvicorn"/
    # "uvicorn.access"/"uvicorn.error" loggers (and any other library may
    # do the same) *before* create_app() runs -- clearing them and forcing
    # propagate=True is what makes redirecting the root logger below
    # actually catch everything, instead of records printing twice: once
    # from a library's own handler, once forwarded through root.
    #
    # `.disabled` belongs to the same reclaim and was missing until 2026-08-10:
    # `logging.config.fileConfig`/`dictConfig` default `disable_existing_loggers`
    # to True and set it on every logger their own config does not name, and
    # `Logger.handle` checks it *below* both the level check and the handler
    # walk -- so a logger left disabled is unreachable no matter what this
    # function does to handlers, levels or sinks. Reached here through
    # `db/migrations/env.py`'s `fileConfig` call (alembic.ini names only root,
    # sqlalchemy and alembic), which is why the whole test suite could not see
    # an httpx WARNING after it migrated in-process. Snapshot the keys: a
    # `getLogger` on a `PlaceHolder` entry can insert parent placeholders, and
    # that would be a mutation during iteration.
    for name in list(logging.root.manager.loggerDict):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True
        stdlib_logger.disabled = False
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # **`httpx` logs one INFO line per request, and the redirect above is what
    # made it visible.** Measured 2026-08-07 on the shipped defaults
    # (`USHER_LOG_JSON=true`, `USHER_LOG_LEVEL=INFO`, sink `sys.stdout`): a
    # single request prints a ~900-character JSON envelope reading
    # `httpx._client:_send_single_request - HTTP Request: POST … "HTTP/1.0
    # 200 OK"` on **stdout**, which is where every CLI command puts its
    # answer. `usher curate` therefore opened with a log record about its own
    # completion before printing the report -- exactly the interleaving
    # `_print_home_report`'s printed-not-logged rule exists to prevent, and
    # the reason `usher search` and `usher curate` pass `report=False` to
    # their factories in the first place. That call turns off *Usher's* line
    # and could do nothing about this one.
    #
    # WARNING and above still arrive, so a real failure is not silenced. And
    # nothing is lost that PRD 10 depends on: `configure_tracing` instruments
    # `httpx` unconditionally, so every one of these requests is already a
    # client span carrying method, URL and status -- this line was a second,
    # unstructured copy of a fact the trace holds better. Verified no test or
    # script in this repository reads it (`grep -rn "HTTP Request"` finds
    # nothing outside `httpx` itself).
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_tracing(settings: Settings) -> None:
    """Install a real SDK `TracerProvider` unconditionally and instrument
    SQLAlchemy + httpx globally, so any span started anywhere in the
    process — including by FastAPI's auto-instrumentation, wired in
    `create_app` — gets a real trace/span id for `inject_trace_context` to
    correlate, whether or not there is anywhere to export it to. Verified
    directly: a bare `TracerProvider()` with zero span processors still
    assigns valid, random ids to spans started through it — only the
    actual OTLP *export* needs `settings.telemetry_enabled`, not span
    creation. Without this, no span is ever active during request
    handling and `inject_trace_context` never fires outside tests that
    build their own span (confirmed directly against a plain, uninstrumented
    request: `get_current_span().get_span_context().is_valid` was `False`).

    Idempotent by construction, and this matters: `create_app()` calling
    this is not a once-per-process event (the test suite alone calls it
    dozens of times). `trace.set_tracer_provider()` silently refuses every
    call after the first in a process, so unconditionally constructing a
    new provider + processor on every call would leak a `BatchSpanProcessor`
    daemon thread and gRPC channel each time, with no handle left to shut
    them down — verified directly: without the `isinstance` guard below,
    five `create_app()` calls with telemetry enabled left five orphaned
    threads. `SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor` need no
    equivalent guard: both are process-wide singletons (verified directly)
    with their own built-in re-instrumentation guard, so calling
    `.instrument()` repeatedly is already a safe no-op after the first time.
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
    """Install a real SDK `MeterProvider`, exporting over OTLP only when
    `settings.telemetry_enabled` -- mirrors `configure_tracing`'s shape for
    the same two reasons: a real (if unexported) provider lets any
    instrument a later milestone creates (`usher.http.server.duration`,
    `usher.jobs.queued`, ... -- PRD 10's metric catalogue) bind to
    something real from day one instead of the API's no-op default, and
    the same `isinstance` idempotency guard avoids leaking a
    `PeriodicExportingMetricReader` background export thread across
    repeated `create_app()` calls the way an unguarded `configure_tracing`
    did (see its docstring; verified directly that `set_meter_provider`
    has the identical silently-refuse-the-second-call behaviour
    `set_tracer_provider` does).

    No metrics are registered here — PRD 10's OTel metrics are each owned
    by the milestone that emits them (M5 push, M6 search, ...). This is
    only the bootstrap they register against, so *where that bootstrap
    lives* is a decision made once here rather than independently in each
    of nine milestones.
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

# Set by `register_queue_gauges`, read by the two callbacks below. A module
# global rather than a closure captured at instrument-creation time, and that
# is load-bearing: the SDK keeps only the *first* observable gauge registered
# under a given name (verified directly -- a second
# `create_observable_gauge("usher.jobs.queued", callbacks=[other])` against
# the same provider is silently discarded and the first callback keeps
# reporting). A composition root that registered twice, or a second test in
# the same process, would otherwise be reading a queue that no longer exists.
_queue_reader: QueueReader | None = None


def register_queue_gauges(read: QueueReader) -> None:
    """PRD 10's `usher.jobs.queued` / `usher.jobs.parked`.

    Observable rather than recorded: the queue's depth is a fact about the
    `jobs` table, not an event stream, and a counter incremented on enqueue
    and decremented on complete drifts the moment anything -- a parked job,
    a requeue, a crash, a `DELETE` from `complete` -- changes a row without
    going through both.

    **`read` is synchronous and returns the caller's most recent full
    re-read of the table, not a query.** The plan asked for a callback that
    "opens its own short-lived session"; that is not implementable here.
    OTel invokes an observable callback from the metric reader's own
    *background thread*, and every database call in this project is a
    coroutine on asyncpg -- so a callback that queried would have to bounce
    a coroutine onto the application's event loop
    (`run_coroutine_threadsafe`) and block the exporter thread on it, which
    deadlocks whenever the loop is itself blocked. What removes the drift
    the plan was worried about is that `read` returns a *complete* re-read
    (`SELECT status, kind, count(*) ... GROUP BY`) rather than a running
    total, so the value is only ever stale, never wrong. `usher work`
    refreshes it after every pass over the queue.

    Safe to call repeatedly, and the *reader* is what makes it so rather
    than a guard on the instruments: a duplicate
    `create_observable_gauge(...)` against a provider that already has one
    is silently discarded by the SDK (verified directly), so a
    re-registration that only created a second instrument would leave the
    first, now-dead reader reporting forever. Creating them unconditionally
    is what lets a *new* `MeterProvider` -- one per test in this suite --
    get instruments of its own instead of orphans bound to a provider that
    has been thrown away.
    """
    global _queue_reader
    _queue_reader = read
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
    return _observations(lambda snapshot: snapshot.queued)


def _observe_parked(options: CallbackOptions) -> Iterable[Observation]:
    return _observations(lambda snapshot: snapshot.parked)


def _observations(
    select: Callable[[QueueSnapshot], Mapping[str, int]],
) -> Iterable[Observation]:
    """No reader means no observation, never a zero.

    A gauge that reported 0 before anything had read the table would be
    indistinguishable from an empty queue, and PRD 10's "ingest stalled"
    alert fires on depth rising rather than on depth being reported -- so a
    fabricated zero is the one value that makes the alert quietly wrong.
    """
    if _queue_reader is None:
        return []
    return [Observation(count, {"kind": kind}) for kind, count in select(_queue_reader()).items()]


@dataclass(frozen=True, slots=True)
class PushSnapshot:
    """One source's push lane, as PRD 10's two series see it.

    **`delivering`, not `connected`.** Dashboard 3's panel is "push
    connection uptime" and its alert is `push.connected == 0` for 15
    minutes, and a series fed by the socket's *state* would be permanently
    green against the failure ADR-0004 measured — a channel that upgraded,
    is held open, and delivers nothing. `usher.source.push.connected` keeps
    PRD 10's name (a metric renamed is a dashboard panel silently blank) and
    reports the honest quantity.

    `reconnects` is cumulative for the lane rather than per connection,
    which is what `PushHealth` being one object across reconnects buys.
    """

    delivering: bool
    reconnects: int


PushReader = Callable[[], Mapping[str, PushSnapshot]]

# A module global, replaced rather than captured -- the SDK keeps only the
# first observable instrument registered under a name and silently discards
# the rest, so a second `create_app()` in one process would otherwise leave
# the first, now-dead reader reporting forever. Same shape, same reason, as
# `_queue_reader` above.
_push_reader: PushReader | None = None


def register_push_gauges(read: PushReader) -> None:
    """PRD 10's `usher.source.push.connected` / `usher.source.push.reconnects`.

    Observable, and this is the one place in this project where that is
    unambiguously safe: the value is an in-memory integer on a `PushHealth`
    ledger, so there is no coroutine to bounce onto the event loop from the
    metric reader's background thread and no exporter thread to block on it.
    `register_queue_gauges` explains at length why the queue's equivalent
    cannot be live; nothing in that argument applies here, and the shape is
    kept identical anyway so a reader meets one pattern rather than two.

    **Two instrument *types*, because PRD 10 documents two.** `connected` is
    a gauge -- 1 or 0, now. `reconnects` is a counter, and an *asynchronous*
    counter is precisely the instrument for a cumulative total read out of a
    ledger rather than incremented at the event. Registering it as a gauge
    would put a different instrument type on the wire under a documented
    name, which is the same class of failure as a near-miss name: the panel
    is there, the series is wrong, and nothing says so.
    """
    global _push_reader
    _push_reader = read
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
    return _push_observations(lambda snapshot: 1 if snapshot.delivering else 0)


def _observe_push_reconnects(options: CallbackOptions) -> Iterable[Observation]:
    return _push_observations(lambda snapshot: snapshot.reconnects)


def _push_observations(select: Callable[[PushSnapshot], int]) -> Iterable[Observation]:
    """No reader means no observation, never a zero.

    A fabricated zero on `usher.source.push.connected` is indistinguishable
    from a source whose channel is down, and PRD 10's "Push down" alert
    fires on exactly that value for fifteen minutes -- so a process that
    reported 0 from start-up until the first lane registered would page
    somebody about a source that was never configured. Same argument
    `_observations` already makes for the queue gauges.
    """
    if _push_reader is None:
        return []
    return [
        Observation(select(snapshot), {"source": source})
        for source, snapshot in _push_reader().items()
    ]


SseReader = Callable[[], int]

# Module global, replaced rather than captured, for the reason `_queue_reader`
# and `_push_reader` above both state: the SDK keeps only the *first*
# observable instrument registered under a name and silently discards the
# rest, so a second `create_app()` in one process would otherwise leave the
# first, now-dead reader reporting forever.
_sse_reader: SseReader | None = None


def register_sse_gauge(read: SseReader) -> None:
    """PRD 10's `usher.sse.connections`.

    **The one observable callback in this project that really is a live
    read.** `register_queue_gauges` explains at length why the queue's
    equivalent cannot be -- OTel invokes the callback from the metric
    reader's background thread and every database call here is a coroutine on
    asyncpg -- and none of that applies to `len()` on an in-memory set. So
    this reader is the bus itself, not a snapshot somebody remembered to
    refresh.
    """
    global _sse_reader
    _sse_reader = read
    metrics.get_meter("usher.api").create_observable_gauge(
        "usher.sse.connections",
        callbacks=[_observe_sse_connections],
        unit="1",
        description="Open SSE client connections",
    )


def _observe_sse_connections(options: CallbackOptions) -> Iterable[Observation]:
    """No reader means no observation, never a zero.

    Same argument `_push_observations` makes: a fabricated zero is
    indistinguishable from a real one, and a process that reported 0 open
    connections from start-up until the first `create_app` finished would be
    reporting a fact it does not have.
    """
    return [] if _sse_reader is None else [Observation(_sse_reader())]


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    """One reading of the embedding backlog.

    Two numbers, because the second is what stops the first being read
    wrongly. `stale` is titles in the embedded population whose vector is
    missing, or was derived from different text, or from a different model --
    the backfill's own predicate, `usher.db.repositories.search.
    STALE_EMBEDDING`. `refused` is titles carrying a row with a **NULL**
    embedding: the deliberate written outcome for a document the composer
    called degenerate.

    A refused title is *not* stale -- `REFUSED_EMBEDDING` is
    `NOT (STALE_EMBEDDING) AND e.embedding IS NULL` precisely so the two
    cannot overlap -- and without the second series an operator watching
    `stale` settle on a nonzero floor cannot tell "the backfill is stuck"
    from "these titles have no text to embed". One is a defect; the other is
    the catalog.

    Both default to 0, and that zero is a *held* one rather than a fabricated
    one: `SearchGauges` reports it only after `register_search_gauges` has
    been handed a reader, and `_search_observations` reports nothing at all
    before that.
    """

    stale: int = 0
    refused: int = 0
    # **A third number, and it is about a different table.** `stale`/`refused`
    # are `title_embeddings`; this is `title_neighbors` rows whose
    # `blend_fingerprint` is not the running one -- M7's fourth similarity
    # signal changed what every stored score *means*, and before that column
    # existed nothing could tell a row computed under the old blend from one
    # computed under the new.
    #
    # It rides on this snapshot rather than on a fourth module global because
    # it is refreshed by exactly the same passes, from the same session, and a
    # separate reader would be a second thing to remember to wire.
    #
    # **A zero here does not mean the artefact is current**, and PRD 10's panel
    # must not be read that way: it means no row disagrees with the running
    # blend. A row can carry the right fingerprint and still be stale because
    # some *other* title was embedded into its neighbourhood since, which is
    # undecidable per row and is why `computed_at()` still exists beside this.
    neighbors_stale: int = 0


SearchReader = Callable[[], SearchSnapshot]

# Module global, replaced rather than captured, for the reason `_queue_reader`,
# `_push_reader` and `_sse_reader` above all state: the SDK keeps only the
# *first* observable instrument registered under a name and silently discards
# the rest, so a second registration in one process would leave the first,
# now-dead reader reporting forever.
_search_reader: SearchReader | None = None


def register_search_gauges(read: SearchReader) -> None:
    """PRD 10's `usher.search.embeddings.stale`, plus its companion.

    Observable rather than recorded, for `register_queue_gauges`' reason: the
    backlog is a fact about a table, not an event stream, and a counter
    incremented at enqueue drifts the moment anything changes a row without
    going through it -- which here is *every enrichment*, since a new overview
    changes the fingerprint and makes a current row stale without touching the
    queue at all.

    **`read` is synchronous and returns the caller's most recent full re-read,
    never a query.** OTel invokes an observable callback from the metric
    reader's own background thread and every database call in this project is
    a coroutine on asyncpg, so a callback that queried would have to bounce a
    coroutine onto the event loop with `run_coroutine_threadsafe` and block
    the exporter thread on it -- a deadlock whenever the loop is itself
    blocked. `composition.SearchGauges` is the held snapshot; the worker lane
    and `usher work` refresh it once per pass, `usher index` after its own
    sweep. The refresh is two `count(*)`s over a predicate that is a
    *predicate* rather than a cursor precisely so counting it stays cheap --
    both are driven off `ix_titles_enrichment_state`, whose population is the
    enriched tier (2k-10k rows) rather than the 1.27M-row catalog.

    Safe to call repeatedly, and the *reader* is what makes it so rather than
    a guard on the instruments -- see `register_queue_gauges` for the whole
    argument, which applies here unchanged.
    """
    global _search_reader
    _search_reader = read
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
    return _search_observations(lambda snapshot: snapshot.stale)


def _observe_embeddings_refused(options: CallbackOptions) -> Iterable[Observation]:
    return _search_observations(lambda snapshot: snapshot.refused)


def _observe_neighbors_stale(options: CallbackOptions) -> Iterable[Observation]:
    return _search_observations(lambda snapshot: snapshot.neighbors_stale)


def _search_observations(select: Callable[[SearchSnapshot], int]) -> Iterable[Observation]:
    """No reader means no observation, never a zero.

    A gauge reporting 0 before anything has read the table is
    indistinguishable from a drained backlog, and "the backfill has drained"
    is the only claim this series exists to support -- it is the one
    observable answer to the milestone's own headline failure, an index that
    does not raise but answers out of date. Same argument `_observations` and
    `_push_observations` already make for their own series.
    """
    if _search_reader is None:
        return []
    return [Observation(select(_search_reader()))]


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
    configure_metrics(settings)


# ---------------------------------------------------------------------------
# The shared cache counters.
#
# These live here rather than beside their first caller because **three caches
# now record through them** -- the row cache and the screen cache in
# `services/rows/cache.py`, and group C's image proxy in `services/images.py`
# -- and a fourth would make it four. Two `create_counter` calls under one
# meter for one instrument name is either a duplicate-instrument warning or a
# second stream, and either way a dashboard's hit rate silently stops covering
# a cache; so the pair is declared exactly once and imported.
#
# 🔴 They were declared in `services/rows/cache.py` until 2026-08-11 and moved
# on the merge that put both callers in one tree: `services/images.py` importing
# the counters while `services/rows/base.py` imported `servable_images` from
# `services/images.py` is a cycle, and it broke collection of 27 test modules.
# Neither task could see it alone -- each half is acyclic. `telemetry.py`
# imports nothing from `usher`, which is what makes it the safe home rather
# than merely a convenient one.
#
# The description names no cache in particular: it said "Row/screen" while the
# pair had one caller, and that was a lie the moment it had two.
_cache_meter = metrics.get_meter("usher.cache")

CACHE_HITS = _cache_meter.create_counter(
    "usher.cache.hits", description="Cache reads that found a live entry"
)
CACHE_MISSES = _cache_meter.create_counter(
    "usher.cache.misses",
    description="Cache reads that found nothing or an expired entry",
)
