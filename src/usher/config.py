"""Application configuration, read from the environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Not a credential — the exact placeholder .env.example ships, kept only to
# detect and reject it. See _reject_placeholder_secret_key below.
_PLACEHOLDER_SECRET_KEY = "change-me-to-a-long-random-string"  # noqa: S105
_ASYNCPG_DRIVER_PREFIX = "postgresql+asyncpg://"


class Settings(BaseSettings):
    """Runtime settings, read from the environment.

    Infrastructure (database, server, secrets, telemetry) is configured here;
    sources are configured at runtime and live in the database.
    """

    model_config = SettingsConfigDict(
        env_prefix="USHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    database_url: SecretStr
    secret_key: SecretStr = Field(min_length=32)

    host: str = "0.0.0.0"  # noqa: S104  intentional: default bind-all for a containerized service
    port: int = Field(default=8000, ge=1, le=65535)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    tmdb_api_key: SecretStr | None = None

    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name: str = Field(default="usher", alias="OTEL_SERVICE_NAME")

    @field_validator("tmdb_api_key", "otlp_endpoint", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """An env var that is present but empty (as `.env.example` ships
        `USHER_TMDB_API_KEY=` and `OTEL_EXPORTER_OTLP_ENDPOINT=`) means
        "not set", not "set to the empty string" — keep `str | None` honest."""
        if isinstance(value, str) and value == "":
            return None
        return value

    @field_validator("secret_key")
    @classmethod
    def _reject_placeholder_secret_key(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() == _PLACEHOLDER_SECRET_KEY:
            raise ValueError(
                "USHER_SECRET_KEY is still the .env.example placeholder — generate a real "
                "value, e.g. `openssl rand -hex 32`"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(_ASYNCPG_DRIVER_PREFIX):
            raise ValueError(
                f"USHER_DATABASE_URL must use the {_ASYNCPG_DRIVER_PREFIX} driver "
                "(the app uses SQLAlchemy's async engine)"
            )
        return value

    @property
    def telemetry_enabled(self) -> bool:
        """Telemetry is optional: with no endpoint configured, exporters are no-ops."""
        return bool(self.otlp_endpoint)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings, built once from the environment.

    Cached because this exists to be a FastAPI `Depends`: without caching,
    every request and every injection site would re-read and re-parse the
    environment (and, once a real `.env` exists, hit disk) for values that
    do not change during the process lifetime. Call `get_settings.cache_clear()`
    to force a rebuild — tests that vary the environment must do this
    explicitly, since the cache otherwise outlives any single test.
    """
    return Settings()
