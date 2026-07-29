"""Regression coverage for the alembic env.py DSN-handling hazard.

env.py must never round-trip the database URL through alembic's
ConfigParser-backed Config (`set_main_option` / `get_main_option` /
`get_section`). A percent-encoded password — RFC 3986 mandates
percent-encoding any password containing `@`, `/`, `:`, `#`, or `%` — makes
`configparser`'s interpolation raise before a single migration runs, and
the raised exception embeds the raw DSN, password included. See env.py's
module docstring and `_database_url()`.
"""

import pytest
from alembic.config import Config

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
