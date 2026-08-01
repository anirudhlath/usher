"""Shared fixtures. Autouse fixtures here define the isolation guarantees
every test in the suite gets for free — this is the first conftest in the
project, and the pattern set here is what later milestones inherit."""

import os
import sys
from collections.abc import Iterator

import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics._internal import _ProxyMeter

from usher.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate every test from the real process environment, from any real
    `.env` file on disk, and from any other test's cached settings.

    Three distinct leaks, all closed here:

    1. Leftover `USHER_*`/`OTEL_*` variables exported in the developer's own
       shell would otherwise leak into `Settings()` calls that don't set
       every field explicitly.
    2. `Settings.model_config["env_file"]` names a *separate* settings
       source that pydantic-settings reads directly off disk. It does not
       go through `os.environ`, so `monkeypatch.delenv(...)` cannot hide a
       real `.env` file — a developer who follows `.env.example` and creates
       one gets test failures that have nothing to do with their change.
       Neutralising the class-level `env_file` config for the duration of
       each test closes that gap without touching the file itself.
    3. `get_settings()` is `@lru_cache`d (it exists to be a FastAPI
       `Depends`), so a previous test's call would otherwise leak its
       cached instance into whatever runs next.
    """
    for key in list(os.environ):
        if key.startswith("USHER_") or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_otel_tracer_provider() -> Iterator[None]:
    """Isolate every test from any real SDK `TracerProvider` a previous
    test (or `usher.telemetry.configure_tracing`, which every `create_app()`
    call runs) installed.

    `opentelemetry.trace.set_tracer_provider()` is deliberately set-once at
    the API level — `configure_tracing`'s own idempotency guard relies on
    exactly that behaviour to avoid leaking a `BatchSpanProcessor` thread
    across repeated `create_app()` calls in one process (see its
    docstring). That same set-once behaviour becomes a test-order
    dependency without this fixture: whichever test in the session
    installs a real provider first "wins" it for every test after.
    Verified directly: without this reset, running a test that calls
    `trace.set_tracer_provider(TracerProvider())` before
    `test_no_trace_context_outside_a_span` makes the latter fail with
    `KeyError('trace_id')` — a stale, still-valid span context leaks in
    from the earlier test's span instead of the "no active span" state
    the test's name promises.

    There is no public "unset" API — a real deployment is never meant to
    call `set_tracer_provider` more than once per process. Reaching into
    the module's private `_TRACER_PROVIDER`/`_TRACER_PROVIDER_SET_ONCE`
    state is the same trick OpenTelemetry's own test suite uses for this.

    **Clearing the global provider is not enough on its own**, which is
    what the second half below is for. `usher.adapters.emby.session` and
    friends call `trace.get_tracer(...)` at *import* time, when no real
    provider exists yet, so each holds a `ProxyTracer` — and a `ProxyTracer`
    caches the first real provider it ever resolves in `_real_tracer` and
    never looks at the global again (verified against the installed SDK's
    own source; an in-code comment in
    `tests/unit/test_adapters_emby_session.py` previously claimed it
    resolved per call, which is wrong). Whichever test first starts a span
    through one of those tracers while a real provider is installed
    therefore owns that tracer for the rest of the session, and every later
    test's in-memory exporter silently receives nothing. Reproduced
    directly: adding a span-capturing integration test for the admin
    routes — which reaches `EmbySession` through the real adapter, earlier
    in collection order — made
    `test_every_upstream_request_produces_a_span` fail with an empty span
    list while it still passed in isolation.
    """

    def _reset() -> None:
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE = type(trace._TRACER_PROVIDER_SET_ONCE)()
        for name, module in list(sys.modules.items()):
            if not name.startswith("usher"):
                continue
            for value in vars(module).values():
                if isinstance(value, trace.ProxyTracer):
                    value._real_tracer = None

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def reset_otel_meter_provider() -> Iterator[None]:
    """`reset_otel_tracer_provider`'s twin, for metrics, and it fails in a
    louder way than the tracer one does.

    `metrics.set_meter_provider()` is set-once exactly like its tracing
    counterpart -- `configure_metrics`'s own `isinstance` guard depends on
    that (see its docstring) -- and every `usher` module that emits a metric
    calls `metrics.get_meter(...)` at *import* time, when no real provider
    exists yet, so each holds a `_ProxyMeter` whose instruments are
    `_Proxy*` shells. A `_ProxyMeter` caches the first real meter it is ever
    handed, and each proxy instrument caches the first real instrument, so
    whichever test installs a real `MeterProvider` first owns every
    module-level instrument in the process for the rest of the session.

    Verified directly, and the failure is not a subtle one: three rounds of
    "install a `MeterProvider` with an `InMemoryMetricReader`, record
    through `usher.services.jobs._job_duration`, read the reader" print the
    metric once and then raise `AttributeError: 'NoneType' object has no
    attribute 'resource_metrics'` -- the SDK logs "Overriding of current
    MeterProvider is not allowed", the second `set_meter_provider` is a
    no-op, and the second reader is never registered with any provider at
    all so `get_metrics_data()` answers `None`. With this reset in place the
    same three rounds each report `['usher.jobs.duration']`.

    There is no public unset API, for the same reason there is none for
    tracing: a real deployment installs one provider per process.
    """

    def _reset() -> None:
        metrics._internal._METER_PROVIDER = None
        metrics._internal._METER_PROVIDER_SET_ONCE = type(
            metrics._internal._METER_PROVIDER_SET_ONCE
        )()
        for name, module in list(sys.modules.items()):
            if not name.startswith("usher"):
                continue
            for value in vars(module).values():
                if not isinstance(value, _ProxyMeter):
                    continue
                value._real_meter = None
                for instrument in value._instruments:
                    instrument._real_instrument = None

    _reset()
    yield
    _reset()
