"""Logging and tracing setup.

Telemetry is optional: with no OTLP endpoint configured the exporters are
no-ops and Usher runs normally. See PRD 10 and ADR-0007.
"""

import sys
from collections.abc import Mapping
from typing import Any

from loguru import logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from usher.config import Settings


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


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
