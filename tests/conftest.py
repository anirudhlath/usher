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

# **`tests/contract/*_contract.py` does not match `python_files`, so nothing rewrites
# its assertions unless this line does.** Every shared contract suite in this repository
# lives in a module pytest never collects -- the subclasses under `tests/unit/` and
# `tests/integration/` are what get collected, and the suite itself is only ever
# *imported* by them.
pytest.register_assert_rewrite("tests.contract")


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
    """Isolate every test from any real SDK `TracerProvider` a previous test (or
    `usher.telemetry.configure_tracing`, which every `create_app()` call runs)
    installed.
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
    """`reset_otel_tracer_provider`'s twin, for metrics, and it fails in a louder way than
    the tracer one does.
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
