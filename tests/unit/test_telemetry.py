from typing import Any

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from usher.api.app import create_app
from usher.config import Settings
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


async def test_a_request_through_the_app_produces_a_valid_span() -> None:
    """The whole point of Task 11 is trace-correlated logs, which needs a
    real, valid span active during request handling. Without FastAPI/
    SQLAlchemy/httpx auto-instrumentation wired into create_app, nothing
    ever starts one -- confirmed directly (a plain request against an
    uninstrumented app leaves get_current_span().get_span_context().
    is_valid False, so inject_trace_context has nothing to inject, ever,
    in the running service). This installs an in-memory exporter *before*
    create_app() runs, so configure_tracing's idempotency guard (see its
    docstring) leaves this provider in place rather than replacing it,
    and asserts the /health request actually produced a recorded, valid
    span -- proof the wiring fires end-to-end, not just that the library
    calls don't raise.

    Uses /health, not /health/ready, specifically so this stays a unit
    test with no real Postgres: create_app's lifespan builds an engine
    from database_url but never connects until something executes a
    query, and liveness never does.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
    )
    app = create_app(settings)

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 200
    spans = exporter.get_finished_spans()
    assert len(spans) >= 1
    assert all(span.context is not None and span.context.is_valid for span in spans)
