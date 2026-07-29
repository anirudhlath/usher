import pytest
from pydantic import ValidationError

from usher.config import Settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_PORT", "9001")
    settings = Settings()
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/usher"
    assert settings.port == 9001


def test_missing_database_url_raises(monkeypatch):
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_masked_in_repr(monkeypatch):
    monkeypatch.setenv(
        "USHER_DATABASE_URL",
        "postgresql+asyncpg://u:extremely-secret-password@db:5432/usher",
    )
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    dump = repr(Settings())
    assert "extremely-secret-password" not in dump
    assert "s" * 32 not in dump


def test_settings_reject_short_secret_key(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_placeholder_secret_key(monkeypatch):
    """.env.example must be unusable as-is: copying its placeholder verbatim
    would ship a credential-encryption key published in the repo."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "change-me-to-a-long-random-string")
    with pytest.raises(ValidationError):
        Settings()


def test_telemetry_disabled_when_no_endpoint(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert Settings().telemetry_enabled is False


def test_blank_tmdb_api_key_is_none(monkeypatch):
    """USHER_TMDB_API_KEY= (present but empty, as .env.example ships it) must
    parse to None, not '' — otherwise `is not None` checks take the wrong
    branch."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_TMDB_API_KEY", "")
    assert Settings().tmdb_api_key is None


def test_blank_otlp_endpoint_is_none(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    settings = Settings()
    assert settings.otlp_endpoint is None
    assert settings.telemetry_enabled is False


def test_unknown_field_in_env_file_rejected(tmp_path):
    """extra='forbid' catches typos like USHER_LOG_LEVL in a real .env file.

    Note the scope: pydantic-settings' EnvSettingsSource looks up each
    declared field's expected name in os.environ rather than scanning it, so
    it can never notice an unrecognized key — only DotEnvSettingsSource (the
    `.env` *file* reader) does the extra scan that extra='forbid' needs to
    catch something. A same-shaped typo exported directly in the shell is
    not caught by this mechanism; there is no test for that because there
    is nothing that would make it pass.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "USHER_DATABASE_URL=postgresql+asyncpg://u:p@db:5432/usher\n"
        f"USHER_SECRET_KEY={'s' * 32}\n"
        "USHER_LOG_LEVL=DEBUG\n"
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=str(env_file))


def test_log_level_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_LOG_LEVEL", "NOPE")
    with pytest.raises(ValidationError):
        Settings()


def test_port_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_rejects_wrong_driver(monkeypatch):
    """A sync postgresql:// URL must fail fast at config load, not deep
    inside SQLAlchemy's async engine much later."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    with pytest.raises(ValidationError):
        Settings()
