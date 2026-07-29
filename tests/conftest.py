"""Shared fixtures. Autouse fixtures here define the isolation guarantees
every test in the suite gets for free — this is the first conftest in the
project, and the pattern set here is what later milestones inherit."""

import os

import pytest

from usher.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the real process environment and from any
    real `.env` file on disk.

    Two distinct leaks, both closed here:

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
    """
    for key in list(os.environ):
        if key.startswith("USHER_") or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
