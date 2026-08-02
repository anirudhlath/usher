"""Application configuration, read from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Not a credential -- a placeholder value kept only to detect and reject it.
# .env.example itself ships USHER_SECRET_KEY= (blank, not this string) so a
# fresh copy fails validation for a different, more obvious reason (a missing
# required field) -- this guards the case where someone instead pastes in a
# placeholder shown in documentation, an old README, or a setup guide.
# See _reject_placeholder_secret_key below.
_PLACEHOLDER_SECRET_KEY = "change-me-to-a-long-random-string"  # noqa: S105
_ASYNCPG_DRIVER_PREFIX = "postgresql+asyncpg://"

_ENV_PREFIX = "USHER_"

# The one sub-namespace inside `USHER_` that `Settings` deliberately does not
# claim, and the reason it has to exist.
#
# **`.env` has two readers with different vocabularies.** Docker Compose reads
# it to substitute `${...}` into `compose.yml`; pydantic-settings reads the
# same file as a settings source, with `extra="forbid"` below. So a variable
# that is *only* meaningful to compose -- the host-side publish port, say --
# is an extra input to `Settings`, and shipping one in `.env.example` made
# `cp .env.example .env`, the README's own first step, fail every entry point
# with `ValidationError: usher_host_port -- Extra inputs are not permitted`.
# `uv run pytest`, `usher bootstrap-status` and `usher push --probe` alike.
#
# The obvious repairs are all worse. `extra="ignore"` would fix it by
# discarding `USHER_LOG_LEVL=DEBUG` too, turning a typo from a startup failure
# into a line in `.env` that silently does nothing -- the same "dead config
# that looks like a control" shape one layer down. Splitting the file gives
# compose no place to read from (compose substitutes from `.env` and nowhere
# else, short of `--env-file` on every invocation). Renaming the one offending
# key fixes today and leaves the next compose variable free to reintroduce it.
#
# So the two readings are separated by *name*: anything under
# `USHER_COMPOSE_` belongs to `compose.yml` and is dropped before validation,
# and everything else under `USHER_` is a setting or a typo. That is a rule a
# future compose variable can satisfy rather than a list somebody has to
# remember to extend -- and `tests/unit/test_deployment_config.py` fails if
# one is added outside the namespace, from either `.env.example`'s side or
# `compose.yml`'s.
COMPOSE_ONLY_PREFIX = "USHER_COMPOSE_"


def _is_compose_only(key: object) -> bool:
    """Whether a settings-source key belongs to `compose.yml` rather than here.

    Both spellings, deliberately. pydantic-settings' dotenv source hands an
    unmatched variable back under its **full** lowercased name
    (`usher_compose_host_port`, which is what the `extra_forbidden` error
    named), while a matched field arrives with the prefix stripped. Accepting
    the stripped form too costs nothing and means a future version of
    pydantic-settings that normalises extras the other way cannot silently
    re-break the README's first step. `test_no_setting_hides_inside_the_
    reserved_namespace` is what keeps the second branch from ever swallowing
    a real field.
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered.startswith(COMPOSE_ONLY_PREFIX.lower()) or lowered.startswith(
        COMPOSE_ONLY_PREFIX.removeprefix(_ENV_PREFIX).lower()
    )


class Settings(BaseSettings):
    """Runtime settings, read from the environment.

    Infrastructure (database, server, secrets, telemetry) is configured here;
    sources are configured at runtime and live in the database.
    """

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
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

    # The push lane and the worker lane (PRD 03, PRD 01's concurrency
    # model). Same reasoning as every block above: PRD 08's TOML config
    # layer does not exist yet. Deliberately named `push_*` rather than
    # `websocket_*` -- a WebSocket is how *Emby* implements push, and
    # config.py is not an adapter.
    #
    # The two lane switches exist because PRD 01 promises "a `--worker`
    # entrypoint flag ... so lanes can be moved to a separate container
    # later by editing compose, with no code change". These are that flag,
    # as configuration rather than as an argument, so one image serves an
    # all-in-one deployment and a split one. Read by
    # `usher.api.lanes.LaneSupervisor.start`.
    push_enabled: bool = True
    worker_enabled: bool = True
    # How long a push channel may deliver *nothing at all* before it is torn
    # down and reconnected. Not a socket timeout: `websockets`'
    # ping_interval/ping_timeout already detect a dead TCP peer, and this
    # detects a live one that has stopped delivering -- the failure ADR-0004
    # measured when a handshake against a nonexistent path was upgraded and
    # held open. `ge=5.0` rather than `gt=0`: Emby's own `Sessions` interval
    # is the subscription's `0,1000` (one second), and a window shorter than
    # that reconnects a healthy channel forever. Both defaults mirror
    # `usher.adapters.emby.push`'s own constants, which are what an adapter
    # built without them gets; `tests/unit/test_config.py` pins the pair
    # together so they cannot drift.
    push_stale_after_seconds: float = Field(default=90.0, ge=5.0)
    # How long one `recv` waits before reporting "nothing yet", which is the
    # tick the staleness watchdog runs on. Small enough that a channel
    # crossing the window above is noticed within a tick of doing so.
    push_poll_seconds: float = Field(default=5.0, gt=0)
    # The base of the reconnect backoff, before jitter, and its ceiling.
    # Equal jitter, the same shape `job_backoff_seconds` drives one lane
    # over -- PRD 08's argument for it against full jitter transfers
    # unchanged, and so does `gt=0`: a zero base collapses the schedule to
    # "reconnect immediately", which is the hot loop it exists to prevent.
    push_backoff_seconds: float = Field(default=5.0, gt=0)
    push_max_backoff_seconds: float = Field(default=300.0, gt=0)
    # PRD 08: "after N failures mark `supports_push = false` and lean on the
    # nightly walk". `ge=1` for the reason `job_max_attempts` has one: a
    # ceiling of zero disables push on the first blip, before one reconnect
    # has been attempted.
    push_max_consecutive_failures: int = Field(default=5, ge=1)
    # How many items one push event may name before the lane stops resolving
    # them one at a time and asks for a delta walk instead. Emby emits
    # `LibraryChanged` during a library scan and it can name thousands;
    # against 1,126,789 items at 1-5 s per request, a request per changed
    # item is a design defect rather than a slow path. Bounded above at 500,
    # because a value an operator could set to 100,000 turns the guard off
    # while looking configured.
    push_max_items_per_event: int = Field(default=50, ge=1, le=500)
    # The floor between two gap-closing delta walks. A flapping socket plus
    # one delta per reconnect is a paged walk of everything changed since
    # the cursor, every few seconds. `ge=0` and not `gt=0`, the one
    # deliberate exception in this block: zero means "close the gap on every
    # reconnect", which is expensive and correct, unlike every other zero
    # here.
    push_gap_min_interval_seconds: float = Field(default=60.0, ge=0)
    # How often the lane supervisor re-reads the source list, so a source
    # added through `POST /admin/sources` gets a lane without a restart.
    push_source_refresh_seconds: float = Field(default=60.0, gt=0)

    # The client event channel (PRD 07's SSE surface). Same reasoning as
    # every block above: PRD 08's TOML config layer does not exist yet.
    # Deliberately named `sse_*` rather than `events_*` -- the knob is about
    # the wire protocol's own idle behaviour, not about the bus.
    #
    # A comment line every this many seconds on an otherwise idle stream.
    # nginx closes an idle connection at 60 s and Cloudflare at ~100 s
    # (ADR-0004's operational facts, which are about an idle HTTP connection
    # and apply to a long-lived response exactly as they apply to a
    # WebSocket), so `lt=60` is a compliance bound expressed as a type. Read
    # by `usher.api.routers.events`; the two below by `create_app`, which is
    # what builds the bus.
    sse_heartbeat_seconds: float = Field(default=20.0, gt=0, lt=60.0)
    # How many events the replay ring holds, for `Last-Event-ID`. A client
    # offline for longer than this is answered `resync_required` rather than
    # replayed a partial stream, because replaying what is left and calling
    # it a resume loses the events that fell off the front silently.
    sse_buffer_size: int = Field(default=256, ge=1, le=10_000)
    # Per-subscriber queue depth. On overflow the subscriber's queue is
    # emptied and replaced with one `resync_required` (PRD 07), so this is a
    # tolerance for a slow client rather than a delivery guarantee.
    sse_queue_size: int = Field(default=64, ge=1, le=10_000)

    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name: str = Field(default="usher", alias="OTEL_SERVICE_NAME")

    @model_validator(mode="before")
    @classmethod
    def _drop_compose_only_variables(cls, values: Any) -> Any:
        """Compose's half of `.env` is not this model's business.

        Before validation rather than as an `extra="ignore"`, so the keys
        `Settings` *does* claim are still validated exhaustively -- see
        `COMPOSE_ONLY_PREFIX` above for why the distinction is by name.
        """
        if not isinstance(values, dict):
            return values
        return {key: value for key, value in values.items() if not _is_compose_only(key)}

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
