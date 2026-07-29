"""Application configuration, read from the environment."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Infrastructure comes from the environment; sources
    are configured at runtime and live in the database."""

    model_config = SettingsConfigDict(
        env_prefix="USHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr
    secret_key: SecretStr = Field(min_length=32)

    host: str = "0.0.0.0"
    port: int = 8000

    log_level: str = "INFO"
    log_json: bool = True

    tmdb_api_key: SecretStr | None = None

    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name: str = Field(default="usher", alias="OTEL_SERVICE_NAME")

    @property
    def telemetry_enabled(self) -> bool:
        """Telemetry is optional: with no endpoint configured, exporters are no-ops."""
        return bool(self.otlp_endpoint)


def get_settings() -> Settings:
    return Settings()
