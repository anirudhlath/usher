"""Driving a `usher` subcommand in a unit test, with no database and no socket."""

from typing import Any

import pytest

from usher.cli import main

#: A DSN that parses and reaches nothing, so a case about a command cannot
#: become a case about its settings.
UNREACHABLE = "postgresql+asyncpg://u:p@127.0.0.1:1/usher"


def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two variables `Settings` has no default for."""
    monkeypatch.setenv("USHER_DATABASE_URL", UNREACHABLE)
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)


def dispatched(
    monkeypatch: pytest.MonkeyPatch, *, arm: str, argv: list[str]
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Run `main(argv)` with `usher.cli.<arm>` recording and the server fatal.
    Returns the `(args, kwargs)` of every call the arm took.

    **`_dispatch`'s `else` arm is `serve`**, so a subcommand that parses and
    has no arm of its own does not fail -- it silently starts the HTTP server
    and looks like it worked, because the server does start. The CLI-wide
    boundary sweep cannot see that: it makes every dispatch coroutine *and*
    `uvicorn.run` raise identically on purpose, so the two arms are
    indistinguishable by construction. Here they are made to differ.

    The arguments come back rather than a count because an arm that reached
    the right coroutine and dropped a flag is the defect a spy that only
    counts cannot see.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    def _served(*_: object, **__: object) -> None:
        raise AssertionError(f"usher {argv[0]} started the HTTP server")

    monkeypatch.setattr(f"usher.cli.{arm}", _record)
    monkeypatch.setattr("uvicorn.run", _served)
    main(argv)
    return calls
