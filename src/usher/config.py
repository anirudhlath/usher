"""Application configuration, read from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Not a credential -- a placeholder value kept only to detect and reject it.
# .env.example itself ships USHER_SECRET_KEY= (blank, not this string) so a
# fresh copy fails validation for a different, more obvious reason (a missing
# required field) -- this guards the case where someone instead pastes in a
# placeholder shown in documentation, an old README, or a setup guide.
# See _reject_placeholder_secret_key below.
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

    # Bulk bootstrap (PRD 04, Phases 0-2). PRD 08 puts knobs like these in a
    # TOML config layer that does not exist yet; until it does, they live here
    # as environment settings rather than as constants nothing can tune.
    # Every one is read by `usher.cli`, the only caller -- none is a field
    # that validates and then influences nothing.
    #
    # Dataset base URLs are deliberately *not* here: they are module constants
    # in their adapters, because a host moving is a code change, not an
    # operator knob. `wikidata_endpoint` is the exception because WDQS has
    # documented mirrors and a self-hosted form.
    bulk_data_dir: Path = Path("data/bulk")
    bulk_batch_size: int = Field(default=50_000, ge=1)
    wikidata_endpoint: str = "https://query.wikidata.org/sparql"
    # WDQS's user-agent policy requires a descriptive agent naming the tool
    # and a way to contact its operator; the default names the project, and an
    # operator running at scale is expected to add their own contact.
    bulk_user_agent: str = Field(
        default="Usher/0.1 (+https://github.com/anirudhlath/usher)", min_length=1
    )

    # Source adapters (PRD 03). Same reasoning as the bulk settings above:
    # PRD 08 puts knobs like these in a TOML config layer that does not
    # exist yet. Deliberately named `source_*`, not `emby_*` -- config.py is
    # not an adapter, and a setting named for one media server would be the
    # first source-specific concept to escape `adapters/`.
    source_page_size: int = Field(default=200, ge=1, le=1000)
    source_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a rejected credential is remembered before another
    # authentication is attempted. Without this, a source configured with a
    # wrong password turns every request into two (the call, then a doomed
    # re-authentication) for as long as it stays wrong.
    source_reauth_cooldown_seconds: float = Field(default=60.0, ge=0)

    # The ingest pipeline (PRD 03). Same reasoning as the bulk and source
    # settings above: PRD 08's TOML config layer does not exist yet.
    sync_batch_size: int = Field(default=1_000, ge=1, le=50_000)
    # The fraction of a source's items one reconcile may mark unavailable
    # before it refuses and changes nothing (ADR-0015). 1.0 disables the
    # guard, which is what an operator deliberately removing a library
    # passes on the command line.
    sync_max_retract_fraction: float = Field(default=0.25, ge=0.0, le=1.0)
    job_batch_size: int = Field(default=20, ge=1, le=500)
    # PRD 08's "after N attempts a job is parked with its error". `ge=1`
    # rather than `ge=0`: a ceiling of zero would park every job on its first
    # failure, which is `retryable=False` applied indiscriminately and takes
    # the retry out of a retry queue.
    job_max_attempts: int = Field(default=5, ge=1)
    # The base of the exponential backoff, before jitter. `gt=0` because a
    # zero base collapses the whole schedule to "retry immediately", which is
    # the hot loop the backoff exists to prevent.
    job_backoff_seconds: float = Field(default=30.0, gt=0)

    # The metadata provider (PRD 03's enrich stage). `tmdb_api_key` above is
    # the credential; these are its tuning. Same reasoning as every block
    # above: PRD 08's TOML config layer does not exist yet.
    #
    # The base URL *is* here, unlike the bulk datasets' hosts, for the reason
    # `wikidata_endpoint` is: TMDb's API has documented alternate hosts
    # (`api.tmdb.org`) and a self-hosted proxy in front of it is a normal
    # deployment for a household behind a restrictive network.
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    # PRD 10's dashboard 3 plots "TMDb requests/sec against the ~40 ceiling
    # with 429 count" -- and TMDb's own documentation puts its limits
    # "somewhere in the 40 requests per second range" without publishing a
    # number. 30 leaves headroom for the retry a 429 triggers without the
    # retry itself becoming the thing that trips the next one.
    tmdb_requests_per_second: float = Field(default=30.0, gt=0)
    # Which certification body's rating lands in `Title.content_rating`.
    # TMDb returns every country's; picking one is configuration, not a
    # constant, because a household outside the US wants its own -- and
    # showing them somebody else's rating is worse than showing none.
    tmdb_region: str = Field(default="US", min_length=2, max_length=2)
    # How long a cached provider payload is reused before the enrich stage
    # refetches. TMDb's licensing term is a *ceiling* of six months rather
    # than a target, so `le=180` is the compliance bound expressed as a type
    # -- a value above it would put the deployment out of terms silently.
    # `ge=1` because zero means "refetch on every attempt", which turns a
    # retry storm into the rate limit it is meant to avoid.
    enrich_cache_max_age_days: int = Field(default=30, ge=1, le=180)

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
                "USHER_SECRET_KEY is still the example placeholder value — generate a real "
                "one, e.g. `openssl rand -hex 32`"
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
