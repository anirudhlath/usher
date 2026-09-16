import logging
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from usher.api.app import create_app
from usher.config import Settings
from usher.telemetry import (
    configure_logging,
    configure_metrics,
    configure_tracing,
    inject_trace_context,
)


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
    """Trace-correlated logs need a real, valid span active during request handling.

    Without FastAPI/SQLAlchemy/httpx auto-instrumentation wired into `create_app`
    nothing ever starts one, so `inject_trace_context` has nothing to inject, ever, in
    the running service. The in-memory exporter is installed *before* `create_app()`
    runs, so `configure_tracing`'s idempotency guard leaves this provider in place, and
    the assertion is that the request actually recorded a valid span rather than that
    the library calls did not raise. Uses `/health` and not `/health/ready` so this
    stays a unit test with no real Postgres: the lifespan builds an engine but never
    connects until something executes a query, and liveness never does.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
        # No lanes: this app exists for the span its request produces, and a
        # push lane would build a real adapter while a worker lane polled a
        # database that is not there. Said per fixture rather than defaulted in
        # `conftest.py`, so it is greppable.
        push_enabled=False,
        worker_enabled=False,
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
    """Exporters must degrade to no-ops when unconfigured.

    Rules out a refactor that hoists the `OTLPSpanExporter` construction above
    `configure_tracing`'s early check. The patched constructor raises if reached at
    all, so this fails loudly rather than merely not asserting anything.
    """

    def _fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError("OTLPSpanExporter must not be constructed when telemetry is disabled")

    monkeypatch.setattr("usher.telemetry.OTLPSpanExporter", _fail_if_constructed)

    settings = _settings_with_telemetry_disabled()
    assert settings.telemetry_enabled is False
    configure_tracing(settings)


def test_diagnose_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`diagnose=True` renders the value of every local a failing traceback line names.

    That prints the plaintext password: SQLAlchemy's `Engine.__repr__` masks the DSN,
    but asyncpg's and SQLAlchemy's own frames pass the parsed connection parameters as
    a dict (`cparams`, `kw`, ...) on their failing line, three frames deep in a library
    this module does not control. PRD 08's "credentials are never logged" rule depends
    on this staying False, so it is asserted rather than trusted to the eye.
    """
    captured: dict[str, object] = {}

    def _capture_add(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(logger, "add", _capture_add)

    configure_logging(_settings_with_telemetry_disabled())

    assert captured["diagnose"] is False
    assert captured["backtrace"] is False


def test_httpxs_per_request_info_line_does_not_reach_the_sink() -> None:
    """A command's answer is stdout, and `httpx` must not write its own line to it."""
    httpx_logger = logging.getLogger("httpx")
    before = httpx_logger.level
    configure_logging(_settings_with_telemetry_disabled())

    sink: list[str] = []
    handler = logger.add(sink.append, level="DEBUG")
    try:
        httpx_logger.info('HTTP Request: POST http://model/v1/chat/completions "HTTP/1.1 200 OK"')
        assert sink == [], f"httpx's per-request line reached the sink: {sink}"

        httpx_logger.warning("Connection pool is full, discarding connection")
        assert len(sink) == 1, "a real httpx problem was silenced along with the noise"
        assert "Connection pool is full" in sink[0]
    finally:
        logger.remove(handler)
        httpx_logger.setLevel(before)


def test_configure_logging_reclaims_a_logger_that_fileconfig_disabled() -> None:
    """Rules out clearing handlers and levels while leaving `.disabled` standing.

    One `fileConfig` call would otherwise mute a logger permanently.
    """
    httpx_logger = logging.getLogger("httpx")
    before_level, before_disabled = httpx_logger.level, httpx_logger.disabled
    httpx_logger.disabled = True

    try:
        configure_logging(_settings_with_telemetry_disabled())
        assert httpx_logger.disabled is False, "configure_logging left the logger disabled"

        sink: list[str] = []
        handler = logger.add(sink.append, level="DEBUG")
        try:
            httpx_logger.warning("Connection pool is full, discarding connection")
            assert len(sink) == 1, "a reclaimed logger still reached no sink"
        finally:
            logger.remove(handler)
    finally:
        httpx_logger.disabled = before_disabled
        httpx_logger.setLevel(before_level)


def test_no_metric_exporter_constructed_when_telemetry_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same invariant as the tracing case, for `configure_metrics`.

    The two bootstraps mirror each other's shape deliberately, so they get the same
    regression test.
    """

    def _fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "OTLPMetricExporter must not be constructed when telemetry is disabled"
        )

    monkeypatch.setattr("usher.telemetry.OTLPMetricExporter", _fail_if_constructed)

    settings = _settings_with_telemetry_disabled()
    assert settings.telemetry_enabled is False
    configure_metrics(settings)


# A port nothing listens on, deliberately, and not the collector's 4317. The two cases
# below assert on what the exporter *constructs* — the channel's `_insecure` flag, and
# whether a processor was attached — and never on whether anything answers.
_DEAD_OTLP_HOST_PORT = "127.0.0.1:1"


def _settings_with_endpoint(endpoint: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
        OTEL_EXPORTER_OTLP_ENDPOINT=endpoint,
    )


def test_a_configured_endpoint_builds_one_real_exporter_over_an_insecure_channel() -> None:
    """The positive mirror of the two "nothing is constructed when disabled" cases.

    Asserts one *installation* rather than one *construction*, because those are
    different failures.
    """
    settings = _settings_with_endpoint(f"http://{_DEAD_OTLP_HOST_PORT}")
    assert settings.telemetry_enabled is True, "the premise: this endpoint enables telemetry"

    configure_tracing(settings)
    configure_metrics(settings)

    tracer_provider = trace.get_tracer_provider()
    assert isinstance(tracer_provider, TracerProvider)
    meter_provider = metrics.get_meter_provider()
    assert isinstance(meter_provider, MeterProvider)

    try:
        processors = tracer_provider._active_span_processor._span_processors
        readers = meter_provider._metric_readers
        assert processors, "no span processor was installed at all"
        assert readers, "no metric reader was installed at all"
        assert len(processors) == 1, f"expected exactly one span processor, got {len(processors)}"
        assert len(readers) == 1, f"expected exactly one metric reader, got {len(readers)}"

        processor = processors[0]
        assert isinstance(processor, BatchSpanProcessor)
        exporter = processor.span_exporter
        assert isinstance(exporter, OTLPSpanExporter)
        assert exporter._insecure is True, (
            "the collector speaks plaintext gRPC, so the channel must be the insecure one"
        )
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_an_endpoint_without_a_scheme_builds_a_secure_channel_against_a_plaintext_collector() -> (
    None
):
    """A bare `host:port` endpoint silently builds a TLS channel.

    With no `insecure=` argument and `OTEL_EXPORTER_OTLP_INSECURE` unset -- which is
    how `telemetry.py` calls it, passing `endpoint=` and nothing else -- the exporter
    defaults `insecure` to `scheme == "http"`. A bare `host:port`, the spelling a
    person types, parses to an empty scheme, builds a TLS channel against a plaintext
    collector, and every export fails inside the SDK's own retry loop, which logs a
    warning and does not raise. The scheme is then discarded and the netloc kept, so
    both spellings store the identical endpoint and this flag is the only observable
    difference. The assertion compares the two spellings rather than pinning `False`,
    so a normalisation that prepends `http://` is noticed.
    """
    settings = _settings_with_endpoint(_DEAD_OTLP_HOST_PORT)
    assert settings.telemetry_enabled is True, "the premise: this endpoint enables telemetry"

    configure_tracing(settings)

    tracer_provider = trace.get_tracer_provider()
    assert isinstance(tracer_provider, TracerProvider)

    try:
        processors = tracer_provider._active_span_processor._span_processors
        assert processors, "no span processor was installed at all"
        processor = processors[0]
        assert isinstance(processor, BatchSpanProcessor)
        without_scheme = processor.span_exporter
        assert isinstance(without_scheme, OTLPSpanExporter)

        with_scheme = OTLPSpanExporter(endpoint=f"http://{_DEAD_OTLP_HOST_PORT}")
        try:
            assert without_scheme._endpoint == with_scheme._endpoint, (
                "the premise: the scheme is discarded, so both spellings target the same netloc "
                "and this flag is the only thing that distinguishes them"
            )
            assert without_scheme._insecure != with_scheme._insecure, (
                "a bare host:port and an http:// endpoint built the same channel -- something "
                "normalises the scheme, and the silent-TLS trap this case pins is gone"
            )
            assert without_scheme._insecure is False
        finally:
            with_scheme.shutdown()
    finally:
        tracer_provider.shutdown()
