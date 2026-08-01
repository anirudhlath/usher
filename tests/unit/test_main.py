"""`usher.__main__` is the container entrypoint (`python -m usher`) -- see
its own module docstring for why it exists instead of the plain `uvicorn`
CLI invocation Task 13's plan text originally showed: it is what makes
`Settings.host`/`Settings.port` actually control where the server binds,
rather than the settings validating and then being read by nothing.
"""

import sys
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
import uvicorn

from usher import __main__ as usher_main
from usher import cli as usher_cli


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


def _dispatched(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """`sys.argv` set as a shell would set it, then `main()` called the way
    a `[project.scripts]` console script calls it -- with no arguments.

    Records which command coroutine `main` dispatched to, or `"serve"` if it
    reached `uvicorn.run`. Each command is replaced by a trivial coroutine
    function rather than patching `asyncio.run`, so the real `asyncio.run`
    still runs -- patching it would reach into the module pytest-asyncio is
    itself driving the loop with.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setattr(sys, "argv", argv)
    seen: list[str] = []

    def recorder(name: str) -> Callable[..., Coroutine[Any, Any, None]]:
        async def run(*args: object, **kwargs: object) -> None:
            seen.append(name)

        return run

    for name in ("_bootstrap", "_status", "_sync", "_sync_status", "_unmatched", "_work"):
        monkeypatch.setattr(usher_cli, name, recorder(name))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.append("serve"))
    usher_cli.main()
    return seen


def test_the_console_script_dispatches_on_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """`[project.scripts] usher = "usher.cli:main"` calls `main()` with **no
    arguments**, so `main` has to read `sys.argv` itself.

    Without this, `argv=None` fell through the same branch as "no arguments
    at all" and `usher sync-status` silently started the HTTP server -- a
    console script that ignores everything it is given, and one that looks
    like it works because the server does start.
    """
    assert _dispatched(monkeypatch, ["usher", "sync-status"]) == ["_sync_status"]
    assert _dispatched(monkeypatch, ["usher", "work", "--once"]) == ["_work"]


def test_the_console_script_with_no_arguments_still_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property `python -m usher` has always had, now shared by both
    entry points: no arguments means run the server, because that is what
    the container's `CMD` does (`alembic upgrade head && exec python -m
    usher`). Adding a console script must not change it."""
    assert _dispatched(monkeypatch, ["usher"]) == ["serve"]
