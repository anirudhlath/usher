from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from usher.telemetry import inject_trace_context


def test_no_trace_context_outside_a_span() -> None:
    record: dict[str, Any] = {"extra": {}}
    inject_trace_context(record)
    assert "trace_id" not in record["extra"]


def test_trace_context_injected_inside_a_span() -> None:
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    record: dict[str, Any] = {"extra": {}}
    with tracer.start_as_current_span("unit"):
        inject_trace_context(record)
    assert len(record["extra"]["trace_id"]) == 32
    assert len(record["extra"]["span_id"]) == 16
