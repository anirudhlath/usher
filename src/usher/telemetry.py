"""Logging and tracing setup.

Telemetry is optional: with no OTLP endpoint configured the exporters are
no-ops and Usher runs normally. See PRD 10 and ADR-0007.
"""

import inspect
import logging
import sys
from collections.abc import Mapping
from typing import Any

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
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
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


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


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
    configure_metrics(settings)
