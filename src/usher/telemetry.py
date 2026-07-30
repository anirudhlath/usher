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
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from usher.config import Settings


def inject_trace_context(record: Mapping[str, Any]) -> None:
    """Patch the active trace and span ids into every log record, so a line
    in Loki links to its trace and back again.

    Typed as the read-only `Mapping` protocol rather than `dict[str, Any]`:
    loguru's own stubs type `logger.configure(patcher=...)` as
    `Callable[[Record], None]`, where `Record` is a `TypedDict` — and a
    TypedDict is not a structural subtype of the invariant `dict[str, Any]`
    (mypy strict rejects passing this function there), but it is one of
    `Mapping[str, Any]`, so this signature satisfies both loguru's real
    `Record` at the `configure()` call site below and the plain
    `dict[str, Any]` this module's unit tests construct without importing
    loguru's internal (stub-only, not present at runtime) `Record` type.
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
    if not settings.telemetry_enabled:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
