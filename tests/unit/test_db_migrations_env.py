"""Regression coverage for the alembic env.py DSN-handling hazard."""

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
    """`Config.set_main_option` rejects a %-bearing DSN at set time.

    Routing the DSN through alembic's `Config` is what this rules out: the raise is a
    plain `ValueError`, before interpolation at get time ever runs.
    """
    config = Config()
    with pytest.raises(ValueError, match="invalid interpolation syntax"):
        config.set_main_option("sqlalchemy.url", _PERCENT_DSN)


def test_the_percent_dsn_would_leak_into_the_configparser_error_message() -> None:
    """The configparser failure embeds the raw DSN, password and all, in its message."""
    config = Config()
    with pytest.raises(ValueError) as exc_info:
        config.set_main_option("sqlalchemy.url", _PERCENT_DSN)
    assert _PERCENT_DSN in str(exc_info.value)


def test_settings_database_url_is_returned_unmangled_regardless_of_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path env.py uses instead.

    Plain `SecretStr` unwrapping, no configparser involved, so the DSN comes back
    byte-for-byte, %-and-all.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", _PERCENT_DSN)
    monkeypatch.setenv("USHER_SECRET_KEY", "0123456789abcdef0123456789abcdef")
    settings = Settings()
    assert settings.database_url.get_secret_value() == _PERCENT_DSN


def test_env_py_never_lets_fileconfig_disable_the_loggers_it_did_not_name() -> None:
    """`fileConfig` must be passed `disable_existing_loggers=False`.

    The default is True, which sets `.disabled` on every logger alembic.ini does not
    name and nothing in `logging` clears again. Read from the source rather than run:
    `fileConfig` against the real alembic.ini would reconfigure root logging for every
    case that ran afterwards, which is the defect rather than a way to observe it.
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
    """`alembic upgrade head` must not print the settings it was handed."""
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
