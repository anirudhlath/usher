"""Regression coverage for the alembic env.py DSN-handling hazard.

env.py must never round-trip the database URL through alembic's
ConfigParser-backed Config (`set_main_option` / `get_main_option` /
`get_section`). A percent-encoded password — RFC 3986 mandates
percent-encoding any password containing `@`, `/`, `:`, `#`, or `%` — makes
`configparser`'s interpolation raise before a single migration runs, and
the raised exception embeds the raw DSN, password included. See env.py's
module docstring and `_database_url()`.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config

import usher.db
from usher.config import Settings

_PERCENT_DSN = "postgresql+asyncpg://usher:p%40ss%25word@localhost:5432/usher"


def test_configparser_round_trip_is_the_hazard_env_py_must_avoid() -> None:
    """Pins the exact failure env.py used to hit: `Config.set_main_option`
    raises immediately -- it doesn't even need a later get_section/
    get_main_option call -- so nobody reintroduces routing the DSN through
    Config. Verified directly: configparser's BasicInterpolation.before_set
    raises a plain ValueError here, not a configparser.Error subclass --
    the failure happens at *set* time, before interpolation proper ever
    runs at get time."""
    config = Config()
    with pytest.raises(ValueError, match="invalid interpolation syntax"):
        config.set_main_option("sqlalchemy.url", _PERCENT_DSN)


def test_the_percent_dsn_would_leak_into_the_configparser_error_message() -> None:
    """The failure mode is worse than a crash: the exception text embeds
    the raw DSN, including the password -- a credentials-in-logs leak."""
    config = Config()
    with pytest.raises(ValueError) as exc_info:
        config.set_main_option("sqlalchemy.url", _PERCENT_DSN)
    assert _PERCENT_DSN in str(exc_info.value)


def test_settings_database_url_is_returned_unmangled_regardless_of_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code path env.py actually uses -- plain SecretStr unwrapping, no
    Config/configparser involved -- must hand back the DSN byte-for-byte,
    %-and-all. This is what makes the fix in env.py's _database_url()
    correct, not just different."""
    monkeypatch.setenv("USHER_DATABASE_URL", _PERCENT_DSN)
    monkeypatch.setenv("USHER_SECRET_KEY", "0123456789abcdef0123456789abcdef")
    settings = Settings()
    assert settings.database_url.get_secret_value() == _PERCENT_DSN


def test_env_py_never_lets_fileconfig_disable_the_loggers_it_did_not_name() -> None:
    """`fileConfig`'s `disable_existing_loggers` defaults to **True**, which
    sets `.disabled` on every logger absent from alembic.ini's `[loggers]`
    (root, sqlalchemy, alembic) -- a migration file silencing modules it has
    no opinion about, permanently, because nothing in `logging` clears that
    flag on reconfigure. Measured 2026-08-10: it is why `pytest tests/unit`
    was green and `pytest tests/integration tests/unit/test_telemetry.py`
    was not. Companion repair in `usher.telemetry.configure_logging`, which
    reclaims the flag whoever set it.

    Structural rather than behavioural, deliberately and in both directions.
    env.py calls this at import under a live alembic context, so a unit test
    cannot reach the call; and `fileConfig` against the real alembic.ini
    would reconfigure root logging for every case that ran afterwards, which
    is the defect rather than a way to observe it. The damage is invisible to
    assertions in this file in any event -- it lands on *other* modules'
    logging.
    """
    source = (Path(usher.db.__file__).parent / "migrations" / "env.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "fileConfig"
    ]
    assert len(calls) == 1, f"expected exactly one fileConfig call in env.py, found {len(calls)}"

    passed = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    disable = passed.get("disable_existing_loggers")
    assert disable is not None, "fileConfig must pass disable_existing_loggers explicitly"
    assert isinstance(disable, ast.Constant) and disable.value is False, (
        "disable_existing_loggers must be False; the default silences every "
        "logger alembic.ini does not name"
    )


def test_alembic_reports_a_rejected_setting_without_printing_any_value() -> None:
    """`alembic upgrade head` must not print the settings it was handed.

    **The second entry point at which `Settings` is read, and it had no
    boundary until 2026-08-13.** `usher.cli` has scrubbed pydantic's
    `input_value={...}` since M7; `env.py` called `get_settings()` bare, so a
    bad `USHER_DATABASE_URL` printed a traceback carrying every field pydantic
    echoes -- including `USHER_SECRET_KEY`, which is not even the setting the
    operator got wrong.

    **This is the site that matters more, and the reason is the Dockerfile.**
    `CMD` is `alembic upgrade head && exec python -m usher`, so on a
    misconfigured container this output is the first thing in the log, emitted
    before the application whose boundary would have caught it ever starts.

    Driven as a **subprocess** rather than by importing `env.py`, which the
    rest of this file cannot do: it touches `alembic.context` at import and
    needs a live migration context. A subprocess is also the only spelling
    that exercises the thing the container actually runs.

    **The environment variable is what makes this deterministic.** A developer
    checkout has a real `.env` supplying a valid DSN, so a case that merely
    *unset* the variable would pass here for the wrong reason and only fail in
    CI. `USHER_DATABASE_URL` set to a wrong-driver DSN is refused by
    `Settings`' own validator whatever `.env` says, and it is the exact shape
    that leaked.

    **Three absences and one presence.** A test that only asserts the password
    is missing is satisfied by a command that prints nothing at all, or that
    fails before it reads the setting -- so the diagnostic half is asserted
    too, and so is the non-zero exit the `&&` above depends on.
    """
    root = Path(usher.db.__file__).parents[2].parent
    # Not a credential -- a canary, so the assertion below can be about a
    # value that could only have come from the environment this test set.
    password = "hunter2xyzzy"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={**os.environ, "USHER_DATABASE_URL": f"mysql://admin:{password}@db:5432/usher"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, "a rejected setting must not be an exit code of 0"
    assert password not in output, f"the DSN's password reached the operator:\n{output}"
    assert "input_value" not in output, f"pydantic's raw rendering reached the operator:\n{output}"
    assert "Traceback" not in output, f"a settings failure is not a stack:\n{output}"
    # The presence half. Without it every assertion above is satisfied by a
    # command that failed for an unrelated reason and printed nothing useful.
    assert "database_url" in output, f"the operator was not told which setting was wrong:\n{output}"
