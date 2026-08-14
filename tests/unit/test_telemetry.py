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


def _settings_with_endpoint(endpoint: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/usher",
        secret_key="0" * 32,
        OTEL_EXPORTER_OTLP_ENDPOINT=endpoint,
    )


def test_a_configured_endpoint_builds_one_real_exporter_over_an_insecure_channel() -> None:
    """The positive mirror of the two "nothing is constructed when disabled"
    cases above, which is the half nobody wrote -- and it asserts one
    *installation* rather than one *construction*, because those are
    different failures. `test_no_exporter_constructed_when_telemetry_disabled`
    monkeypatches the exporter class to raise, so it can only ever say that
    zero were built; a `configure_tracing` that built an exporter and then
    guarded the `add_span_processor` call instead of the exporter satisfies
    it, ships a provider with an empty processor list, and exports nothing.
    That is the state this case exists to tell apart from a working one.

    **The positive control is the point, not decoration.** A provider with
    no span processor and a provider with no metric reader *are* the
    disabled state (`telemetry.py:191-197`, `:222-232` add neither when
    `settings.telemetry_enabled` is false), so an assertion phrased as
    "nothing wrong is attached" is satisfied by nothing being attached at
    all. `assert processors` and `assert readers` run before the counts for
    exactly that reason.

    The provider and the reader are shut down at the end because both own a
    background thread -- see `configure_tracing`'s docstring on the five
    orphaned threads that motivated its idempotency guard. Nothing is
    recorded through either, so both queues are empty and neither shutdown
    reaches the network.

    **The reader count is read off `_metric_readers` and not off
    `_all_metric_readers`, which is a class attribute.** Measured directly:
    `MeterProvider._all_metric_readers` is a `WeakSet` on the *class*, the
    SDK's registry for refusing to bind one reader to two providers, so two
    providers holding one and two readers each both report **3**. The first
    spelling of this case read it and passed alone and under `-k`, then
    failed the whole unit suite with `got 12` -- one weakref per
    `InMemoryMetricReader` any earlier test had built. Same family as this
    repository's standing "a suite run one directory at a time is not the
    suite" finding: an assertion over global state is satisfied by whatever
    else happens to be in the process, and the isolated run is the one that
    lies. `_metric_readers` is the per-provider list (1, 2 and 0 for the
    three providers above).
    """
    settings = _settings_with_endpoint("http://127.0.0.1:4317")
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
    """The scheme is load-bearing and the wrong spelling fails *silently*,
    which is why it gets a case rather than a sentence in a docstring.

    Measured in the installed `opentelemetry-exporter-otlp-proto-grpc`
    1.44.0, `exporter.py:316-323`: with no `insecure=` argument and
    `OTEL_EXPORTER_OTLP_INSECURE` unset -- which is exactly how
    `telemetry.py:195` and `:224` call it, passing `endpoint=` and nothing
    else -- `insecure` defaults to `parsed_url.scheme == "http"`. So
    `127.0.0.1:4317`, the spelling a person types, parses to an empty
    scheme, builds a **TLS** channel against a plaintext collector, and
    every export fails inside the SDK's own retry loop, which logs a
    warning and does not raise. `:325-326` then discards the scheme and
    keeps the netloc, so **both spellings store the identical endpoint**
    and the only observable difference is this flag.

    The assertion is that the flag *differs between the two spellings*
    rather than that this one is `False`: a future normalisation that
    prepends `http://` to a bare endpoint would make the two agree, which
    is the change this case is here to notice.
    """
    settings = _settings_with_endpoint("127.0.0.1:4317")
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

        with_scheme = OTLPSpanExporter(endpoint="http://127.0.0.1:4317")
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
