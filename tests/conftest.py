"""Shared fixtures. Autouse fixtures here define the isolation guarantees
every test in the suite gets for free — this is the first conftest in the
project, and the pattern set here is what later milestones inherit."""

import os
from collections.abc import Iterator

import pytest

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
