"""`usher.__main__` is the container entrypoint (`python -m usher`) -- see
its own module docstring for why it exists instead of the plain `uvicorn`
CLI invocation Task 13's plan text originally showed: it is what makes
`Settings.host`/`Settings.port` actually control where the server binds,
rather than the settings validating and then being read by nothing.
"""

import pytest
import uvicorn

from usher import __main__ as usher_main


def test_main_starts_uvicorn_with_settings_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_HOST", "127.0.0.1")
    monkeypatch.setenv("USHER_PORT", "9009")

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(app: str, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)

    usher_main.main([])

    assert len(calls) == 1
    app, kwargs = calls[0]
    assert app == "usher.api.app:create_app"
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9009


def test_main_defaults_match_dockerfile_expose(monkeypatch: pytest.MonkeyPatch) -> None:
    """No USHER_HOST/USHER_PORT set -- the defaults Settings falls back to
    must match what the Dockerfile EXPOSEs and what compose.yml's usher
    healthcheck probes, or a default deployment silently mismatches."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)

    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    usher_main.main([])

    _, kwargs = calls[0]
    assert kwargs["host"] == "0.0.0.0"  # noqa: S104  matches Settings' own documented default
    assert kwargs["port"] == 8000
