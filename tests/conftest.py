"""Shared fixtures. Autouse fixtures here define the isolation guarantees
every test in the suite gets for free — this is the first conftest in the
project, and the pattern set here is what later milestones inherit."""

import os
from collections.abc import Iterator

import pytest
from opentelemetry import trace

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
    """

    def _reset() -> None:
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE = type(trace._TRACER_PROVIDER_SET_ONCE)()

    _reset()
    yield
    _reset()
