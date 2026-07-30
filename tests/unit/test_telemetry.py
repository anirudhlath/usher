from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from usher.api.app import create_app
from usher.config import Settings
from usher.telemetry import configure_logging, configure_tracing, inject_trace_context


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


def _settings_with_telemetry_disabled() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
    )


def test_no_exporter_constructed_when_telemetry_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Exporters must degrade to no-ops when unconfigured" was previously
    prose, not a test -- nothing caught a stray refactor that hoisted the
    OTLPSpanExporter construction above configure_tracing's early check.
    Monkeypatches OTLPSpanExporter to raise if constructed at all, so this
    fails loudly rather than merely not asserting anything.
    """

    def _fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError("OTLPSpanExporter must not be constructed when telemetry is disabled")

    monkeypatch.setattr("usher.telemetry.OTLPSpanExporter", _fail_if_constructed)

    settings = _settings_with_telemetry_disabled()
    assert settings.telemetry_enabled is False
    configure_tracing(settings)


def test_diagnose_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """diagnose=True renders the *value* of any local variable referenced
    on a traceback frame's failing line. Verified directly against a real
    connection failure (not a synthetic exception): forcing build_engine
    to fail against an unreachable host with diagnose=True printed the
    plaintext password four times over -- not because the DSN string
    itself appears anywhere obvious (SQLAlchemy's own Engine.__repr__
    correctly masks it as `://user:***@host`), but because several of
    asyncpg's and SQLAlchemy's own internal frames pass the parsed
    connection parameters as a dict (`cparams`, `kw`, ...) on their
    failing line, e.g. `dialect.connect(*cargs_tup, **cparams)` -- and
    diagnose renders whatever a failing line references, including a
    dict containing `password: <plaintext>`, three frames deep in a
    third-party library this module doesn't control. PRD 08's
    "credentials are never logged" rule depends on this staying False;
    worth asserting directly rather than trusting it stays correct by eye
    in a file nine milestones will edit.
    """
    captured: dict[str, object] = {}

    def _capture_add(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(logger, "add", _capture_add)

    configure_logging(_settings_with_telemetry_disabled())

    assert captured["diagnose"] is False
    assert captured["backtrace"] is False
