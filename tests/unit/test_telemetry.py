import logging
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
        # No lanes: this app exists for the span its request produces, and a
        # push lane would build a real adapter while a worker lane polled a
        # database that is not there. See `usher.api.lanes`' module
        # docstring -- said per fixture rather than defaulted in
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


def test_httpxs_per_request_info_line_does_not_reach_the_sink() -> None:
    """**A command's answer is stdout, and `httpx` was writing to it.**

    `httpx` logs `HTTP Request: <method> <url> "<status>"` at INFO once per
    request, and `_InterceptHandler` -- correctly -- redirects every stdlib
    record into loguru, whose sink is `sys.stdout` at INFO on the shipped
    defaults. Measured 2026-08-07 against a loopback server: one request put a
    ~900-character JSON envelope on stdout *in front of* the command's own
    output. `usher search` and `usher curate` both pass `report=False` to
    their factories to keep exactly that off the answer, and that call could
    only silence Usher's own line.

    Two arms, and the second is what stops the fix being "log nothing":
    INFO is dropped, WARNING still arrives. Asserted through a **DEBUG** sink,
    so the suppression has to be the stdlib logger's own level and not the
    loguru sink's -- a fix that raised the sink threshold instead would pass
    an INFO-sink version of this case and still print on a deployment running
    `USHER_LOG_LEVEL=DEBUG`.

    Nothing observable is lost: `configure_tracing` instruments `httpx`
    unconditionally, so the same request is already a client span with method,
    URL and status on it.
    """
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
    """**`configure_logging` cleared handlers and levels and left `.disabled`
    standing, so one `fileConfig` call muted a logger permanently.**

    Found 2026-08-10 from CI, and the shape of the failure is the finding:
    `pytest tests/unit` was green, `pytest tests/integration
    tests/unit/test_telemetry.py` failed the httpx case above on its *second*
    arm -- the WARNING that must still arrive. `env.py` calls
    `fileConfig(config.config_file_name)`, whose `disable_existing_loggers`
    defaults to **True**, which sets `.disabled = True` on every logger absent
    from alembic.ini's `[loggers] keys = root,sqlalchemy,alembic`. The
    integration suite migrates in-process, so `httpx` was disabled before the
    unit suite ran.

    `Logger.disabled` is checked in `Logger.handle`, *below* both the level
    check and the handler walk, so nothing `configure_logging` did could
    recover it: the loop cleared every logger's handlers and forced
    `propagate = True` -- exactly to reclaim logging from a library that had
    taken it -- and a disabled logger defeats that as completely as a stray
    handler does, which is why the reclaim now includes it.

    Pinned here rather than by suite order: this case disables the logger
    itself, so it fails on `pytest tests/unit/test_telemetry.py` alone. The
    httpx case is where it surfaced only because its second arm is the rare
    assertion that requires a stdlib record to *arrive*.
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
    """Same invariant as test_no_exporter_constructed_when_telemetry_disabled,
    for configure_metrics's OTLPMetricExporter -- the two bootstraps
    mirror each other's shape deliberately (see configure_metrics's
    docstring), so they get the same regression test.
    """

    def _fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "OTLPMetricExporter must not be constructed when telemetry is disabled"
        )

    monkeypatch.setattr("usher.telemetry.OTLPMetricExporter", _fail_if_constructed)

    settings = _settings_with_telemetry_disabled()
    assert settings.telemetry_enabled is False
    configure_metrics(settings)
