"""Application configuration, read from the environment."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Below this length a rejected value is too short to be worth redacting out of
#: a message and too likely to collide with ordinary words in it -- `"1"` would
#: rewrite half the sentence. Four is the shortest thing this project treats as
#: a secret.
_SHORTEST_REDACTABLE = 4

#: What `LaneSupervisor._close_gap` is allowed to do on a reconnect, and the
#: one setting here whose *default* is a refusal rather than a limit.
PushGapClose = Literal["cursored", "always", "never"]


def settings_rejection(exc: ValidationError, *, entry_point: str) -> str:
    """Pydantic's diagnosis with every rejected value stripped out."""
    lines = [f"{entry_point}: the settings were rejected"]
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(settings)"
        message = error["msg"]
        rejected = str(error.get("input", ""))
        if len(rejected) >= _SHORTEST_REDACTABLE and rejected in message:
            message = message.replace(rejected, "<redacted>")
        lines.append(f"  {where}: {message}")
    lines.append("(values are not shown -- any setting may be a credential)")
    return "\n".join(lines)


# A local OpenAI-compatible server, because this project's reference
# deployment is self-hosted. Declared here rather than in
# `usher.adapters.llm` deliberately: a deployment default is a property of
# the configuration layer, and `OpenAICompatibleClient` takes `base_url` as a
# required argument so it holds no opinion about where it is pointed.
DEFAULT_LLM_BASE_URL = "http://localhost:8000/v1"

# Not a credential -- a placeholder value kept only to detect and reject it.
_PLACEHOLDER_SECRET_KEY = "change-me-to-a-long-random-string"  # noqa: S105
_ASYNCPG_DRIVER_PREFIX = "postgresql+asyncpg://"

_ENV_PREFIX = "USHER_"

# The one sub-namespace inside `USHER_` that `Settings` deliberately does not claim, and
# the reason it has to exist.
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

    The second branch is also what admits compose's *own* variables,
    `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE`, which the README puts in `.env`
    to separate a second stack and to opt into the telemetry network. The
    dotenv source hands them over as `compose_project_name`; without the
    branch they would be refused like any other unknown key.
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

    # The connection pool.
    db_pool_size: int = Field(default=20, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)

    host: str = "0.0.0.0"  # noqa: S104  intentional: default bind-all for a containerized service
    port: int = Field(default=8000, ge=1, le=65535)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    tmdb_api_key: SecretStr | None = None

    # Bulk bootstrap (PRD 04, Phases 0-2).
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
    # The proactive outbound ceiling: calls to one source are spaced at least
    # `1/rate` seconds apart.
    source_requests_per_second: float = Field(default=0.4, ge=0)

    # The ingest pipeline (PRD 03). Same reasoning as the bulk and source
    # settings above: PRD 08's TOML config layer does not exist yet.
    sync_batch_size: int = Field(default=1_000, ge=1, le=50_000)
    # The fraction of a source's items one reconcile may mark unavailable
    # before it refuses and changes nothing.
    sync_max_retract_fraction: float = Field(default=0.25, ge=0.0, le=1.0)
    job_batch_size: int = Field(default=20, ge=1, le=500)
    # How many jobs one worker process may have in flight at once, and the per-kind
    # ceiling for the network-bound kinds.
    job_concurrency: int = Field(default=12, ge=1, le=64)
    # How long a claim may go un-heartbeated before another worker may take it back.
    job_lease_seconds: float = Field(default=300.0, ge=10.0)
    # PRD 08's "after N attempts a job is parked with its error". `ge=1`
    # rather than `ge=0`: a ceiling of zero would park every job on its first
    # failure, which is `retryable=False` applied indiscriminately and takes
    # the retry out of a retry queue.
    job_max_attempts: int = Field(default=5, ge=1)
    # The base of the exponential backoff, before jitter. `gt=0` because a
    # zero base collapses the whole schedule to "retry immediately", which is
    # the hot loop the backoff exists to prevent.
    job_backoff_seconds: float = Field(default=30.0, gt=0)

    # The metadata provider (PRD 03's enrich stage).
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
    # How long a cached provider payload is reused before the enrich stage refetches.
    enrich_cache_max_age_days: int = Field(default=30, ge=1, le=180)

    # Search and embeddings (PRD 05, M6).
    embedding_enabled: bool = False
    # The runtime **and** the checkpoint, because the two are not separable facts about
    # a vector: the same weights served by sentence-transformers and by fastembed differ
    # by 1.41e-03 max pairwise delta, which is 6x the halfvec quantisation error.
    embedding_model: str = Field(default="fastembed:BAAI/bge-large-en-v1.5", min_length=1)
    # On CPU, throughput is flat from 16 to 64 and degrades at 128. `le=512`
    # because the ceiling here is memory, and the cost of being wrong is an OOM
    # inside a worker pass rather than a slow one.
    embedding_batch_size: int = Field(default=16, ge=1, le=512)
    # Read only by the `openai:` runtime, and deliberately **not** reusing
    # `llm_base_url`.
    embedding_base_url: str = Field(default="http://localhost:8001/v1", min_length=1)
    # `SecretStr` per CLAUDE.md, and empty by default because the common case
    # is a local server that wants no key. Empty means no `Authorization`
    # header at all rather than an empty bearer token: a server that ignores
    # the header and one that rejects a blank one are both served correctly by
    # omitting it, and only one of them by sending it.
    embedding_api_key: SecretStr = SecretStr("")
    # A whole batch, not a token. `embedding_batch_size` texts of up to 512
    # tokens each is one request, and a cold model behind a proxy can take
    # seconds to answer the first. Bounded above because an embedder that
    # hangs holds a worker slot, and `JobWorker` has no timeout of its own.
    embedding_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    # Sets `HF_HUB_OFFLINE` before the model library is imported, and it is not a
    # hardening flag.
    embedding_offline: bool = True

    # The LLM (PRD 06's curation, PRD 05's query expansion).
    llm_enabled: bool = False
    # The provider abstraction, and the whole of it.
    llm_base_url: str = Field(default=DEFAULT_LLM_BASE_URL, min_length=1)
    # `SecretStr`, and `None` is a first-class value: a local vLLM or an
    # Ollama needs no credential, and sending `Bearer None` is how a client
    # fails against the deployment this is actually for.
    llm_api_key: SecretStr | None = None
    llm_model: str = Field(default="gpt-4o-mini", min_length=1)
    # The token ceiling on one completion.
    llm_max_output_tokens: int = Field(default=2048, ge=256, le=32_768)
    # A generation is a background job with a whole backoff schedule behind it,
    # so a long timeout costs a worker pass rather than a request. 120 s is
    # roughly 20x the slowest completion this has ever seen.
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    # Cost, in dollars per million tokens, because **no provider reports
    # cost** -- the `usage` object carries token counts and nothing else.
    llm_price_in_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    llm_price_out_per_mtok: Decimal = Field(default=Decimal(0), ge=0)

    # How many candidates one generation's prompt carries.
    curation_pool_size: int = Field(default=200, ge=1, le=1000)

    # PRD 05's query expansion: one completion rewriting the query before
    # `SearchService` embeds it.
    query_expansion_enabled: bool = False

    # The retrieval half. Every one of these is read by
    # `composition.build_pipeline`, which constructs the two indexes and
    # `SearchService`.

    # The ceiling on `SearchRequest.limit`, applied by `SearchService` before a
    # request reaches an index -- not the default, the most a caller may ask
    # for. `le=200` because every candidate becomes a `SearchResult` assembled
    # in application code and RRF fuses two lists of this size; 10,000 is a
    # scan wearing a search's name.
    search_result_limit: int = Field(default=50, ge=1, le=200)
    # Reciprocal Rank Fusion's smoothing constant: `1 / (k + rank)`.
    search_rrf_k: int = Field(default=60, ge=1, le=1000)
    # pgvector's `hnsw.ef_search`, set per statement rather than globally.
    search_hnsw_ef_search: int = Field(default=200, ge=1, le=1000)
    # `pg_trgm`'s `similarity()` floor for the suggest path. Bounded to (0, 1]
    # because that is `similarity()`'s own range: 0 admits every row in
    # `titles` as a candidate, which is the latency cliff PRD 05 says the
    # narrow path exists to avoid, and 1.0 admits only exact matches, which is
    # `LIKE`.
    search_trigram_threshold: float = Field(default=0.3, gt=0.0, le=1.0)
    # How many trigram candidates are collected before the `levenshtein`
    # re-rank. The candidate cut is what keeps edit distance off the whole
    # table. It must exceed `search_result_limit` or the re-rank can only
    # reorder what the cap already chose.
    search_suggest_candidates: int = Field(default=200, ge=1, le=2000)
    # Whether `GET /search/suggest` and `usher suggest` write a
    # `search_queries` row, buffered off the request path. A `bool` and never a
    # sample rate, because a rate adds an absence nobody can name. It narrows
    # the suggest surface only -- turning keystroke analytics off is not a
    # request to stop recording searches.
    search_suggest_analytics: bool = True

    # The push lane and the worker lane (PRD 03, PRD 01's concurrency model).
    push_enabled: bool = True
    worker_enabled: bool = True
    # How long a push channel may deliver *nothing at all* before it is torn down and
    # reconnected.
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
    # How many items one push event may name before the lane stops resolving them one at
    # a time and asks for a delta walk instead.
    push_max_items_per_event: int = Field(default=50, ge=1, le=500)
    # The floor between two gap-closing delta walks.
    push_gap_min_interval_seconds: float = Field(default=60.0, ge=0)
    # What the gap-closer may do when the delta has no cursor; `PushGapClose`
    # above is the vocabulary.
    push_gap_close: PushGapClose = "cursored"
    # The ceiling on **one** gap-closing delta, counted in items, and the other half of
    # the same hazard (M10 S6).
    push_gap_max_items: int = Field(default=20_000, ge=0)
    # How often the lane supervisor re-reads the source list, so a source
    # added through `POST /admin/sources` gets a lane without a restart.
    push_source_refresh_seconds: float = Field(default=60.0, gt=0)

    # The scheduled-work lane: the third lane switch, and the only one that is
    # **off** by default.
    scheduler_enabled: bool = False
    # How long the loop sleeps between ticks.
    scheduler_tick_seconds: float = Field(default=300.0, ge=60.0)

    # The neighbour rebuild's period (M10's J6), the scheduler's second registration.
    similar_rebuild_period_hours: float = Field(default=24.0, ge=1.0)

    # `search_queries` retention (PRD 10's *"nothing owns this table's size"*, M10's
    # J5), the scheduler's first registration.
    search_query_retention_days: int = Field(default=90, ge=1)
    # How many rows one transaction may delete.
    search_query_retention_batch: int = Field(default=10_000, ge=1)

    # The client event channel (PRD 07's SSE surface).
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

    # The image proxy (PRD 07's `GET /images/{id}`, M9).
    image_cache_dir: Path = Path("data/images")
    # The most one CDN answer may be, enforced **while it streams** rather than against
    # a `Content-Length` the sender controls.
    image_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    # How long one CDN fetch may take.
    image_fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    # The provider's image host, `{base}` in the ladder's `{base}{rung}{path}`.
    image_cdn_base_url: str = Field(default="https://image.tmdb.org/t/p/", min_length=1)

    # Whether this process also serves Usher Console at `/console` (see
    # `usher.api.console`).
    console_enabled: bool = True
    # Where the built bundle lives, relative to the working directory exactly as
    # `image_cache_dir` is.
    console_dist_dir: Path = Path("web/dist")
    # The deployment's Grafana, for the Insights screen's "Open in Grafana".
    grafana_url: str | None = None
    # The deployment's Tempo, for the "Open trace" link on a rendered problem document.
    tempo_url: str | None = None

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

    @model_validator(mode="after")
    def _suggest_cap_leaves_room_to_choose(self) -> "Settings":
        """A cap at or below the result limit is a cap that cannot cut.

        `PostgresSuggestIndex` collects `search_suggest_candidates` trigram
        matches, re-ranks them by edit distance, and keeps the best
        `search_result_limit`. With the cap at or below the limit the re-rank
        is handed exactly the rows it is meant to choose *among*, so it can
        reorder but never discard -- and the ordering the type-ahead box shows
        is then whatever the trigram floor happened to admit. That is the
        implementation `test_a_single_character_typo_still_finds_a_short_title`
        and `test_results_are_ordered_by_popularity_within_equal_distance`
        exist to rule out, reachable by configuration rather than by code.

        A cross-field rule because neither field can express it alone, in the
        shape `sse_heartbeat_seconds`' `lt=60.0` established for a constraint
        that *is* expressible: a bound that is a real constraint belongs in
        the type system, wherever it fits. **Not hypothetical** -- both
        ceilings allow `search_result_limit = 200` against the cap's own
        default of 200, so an operator reaches the bad state by raising the
        limit alone, which is the ordinary thing to do.
        """
        if self.search_suggest_candidates <= self.search_result_limit:
            raise ValueError(
                "USHER_SEARCH_SUGGEST_CANDIDATES must exceed USHER_SEARCH_RESULT_LIMIT "
                "-- the edit-distance re-rank has to have more candidates than it keeps"
            )
        return self

    @model_validator(mode="after")
    def _query_expansion_needs_a_client(self) -> "Settings":
        """Refuse the one combination of the two LLM switches that means nothing."""
        if self.query_expansion_enabled and not self.llm_enabled:
            raise ValueError(
                "USHER_QUERY_EXPANSION_ENABLED=true needs USHER_LLM_ENABLED=true "
                "-- query expansion is one completion in front of the embed, and "
                "with no LLM there is no completion to put there"
            )
        return self

    @model_validator(mode="after")
    def _the_pool_can_hold_the_worker(self) -> "Settings":
        """A concurrency the pool cannot serve is refused at startup.

        Every job in flight holds a session, and the worker needs two more of
        its own: the claim and the heartbeat. Over the pool's capacity, jobs do
        not fail fast -- SQLAlchemy's `QueuePool` **waits** `pool_timeout`
        (30 s, the default this project does not change) and then raises, so
        the symptom is a lane that gets slower and slower and finally starts
        parking jobs with a message about a pool. That is a configuration
        mistake wearing an upstream's clothes.

        The bound is deliberately *not* "and leave room for the API": a
        split-container deployment (`USHER_WORKER_ENABLED=false` on the server,
        `usher work` beside it) has no API requests on the worker's pool at
        all, and a validator that assumed otherwise would refuse a correct
        deployment. What it refuses is the arithmetic that cannot work in any
        shape. `db/base.py`'s docstring carries the in-process budget.
        """
        needed = self.job_concurrency + 2
        capacity = self.db_pool_size + self.db_max_overflow
        if needed > capacity:
            raise ValueError(
                f"USHER_JOB_CONCURRENCY={self.job_concurrency} needs {needed} connections "
                f"(one per job in flight, plus the claim and the heartbeat) and "
                f"USHER_DB_POOL_SIZE={self.db_pool_size} + "
                f"USHER_DB_MAX_OVERFLOW={self.db_max_overflow} is {capacity} "
                "-- raise the pool or lower the concurrency"
            )
        return self

    @field_validator(
        "tmdb_api_key", "otlp_endpoint", "llm_api_key", "grafana_url", "tempo_url", mode="before"
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """An env var that is present but empty means "not set".

        `.env.example` ships `USHER_TMDB_API_KEY=` and
        `OTEL_EXPORTER_OTLP_ENDPOINT=` blank, and a local vLLM or Ollama is
        configured with no credential at all, so `USHER_LLM_API_KEY=` is the
        documented way to say so. A `SecretStr("")` is truthy enough to build
        an `Authorization: Bearer ` header, which a strict server rejects with
        a 401 naming a credential the operator never set.
        """
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
        """Telemetry is optional: with no endpoint configured no exporter is constructed."""
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
