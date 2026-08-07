"""Application configuration, read from the environment."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A local OpenAI-compatible server, because this project's reference
# deployment is self-hosted. Declared here rather than in
# `usher.adapters.llm` deliberately: a deployment default is a property of
# the configuration layer, and `OpenAICompatibleClient` takes `base_url` as a
# required argument so it holds no opinion about where it is pointed.
DEFAULT_LLM_BASE_URL = "http://localhost:8000/v1"

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

    # Search and embeddings (PRD 05, M6). Same reasoning as every block
    # above: PRD 08 puts knobs like these in a TOML config layer that does
    # not exist yet. Named `embedding_*` / `search_*` rather than
    # `fastembed_*` or `postgres_*` -- config.py is not an adapter, and
    # ADR-0002's whole position is that the engine behind these is
    # replaceable.
    #
    # **The block arrived in three commits and is one block.**
    # `test_every_setting_is_read_by_something` means a field cannot land
    # ahead of its reader, so the four `embedding_*` came with the embedder
    # and the five `search_*` with the two indexes and `SearchService`.
    # `tests/unit/test_config.py::test_the_search_and_embedding_settings_
    # have_the_measured_defaults` is what holds all nine in one place.
    #
    # **There is deliberately no `index_enabled`.** M6's plan asked for one,
    # "same shape as `push_enabled` / `worker_enabled`", and its stated
    # justification -- a deployment with it off still has full-text and
    # trigram over all 1.27M titles -- is `embedding_enabled`'s
    # justification, word for word. The two would gate the same decision:
    # `composition.build_worker` registers `JobKind.INDEX` on
    # `embedder is not None`, and `composition.embedder` returns `None`
    # exactly when `embedding_enabled` is false, so an `index_enabled` added
    # today has no reachable behaviour of its own -- and
    # `test_every_setting_is_read_by_something` is a substring scan that
    # cannot see that. The pair genuinely separates when the *server* process
    # gains a use for a model the worker lane must not consume, which is M9's
    # search routes; that is when to add it, with a case that fails without
    # it.
    #
    # Off by default, and that is the honest default rather than a cautious
    # one. The dependency lives behind an extra (`uv sync --extra
    # embedding`), so the common install genuinely cannot run index jobs --
    # and PRD 05's catalog-lookup tier, full-text plus trigram over all
    # 1.27M titles, needs no model at all. A deployment with this off is
    # *narrowed*, not broken.
    embedding_enabled: bool = False
    # The runtime **and** the checkpoint, because the two are not separable
    # facts about a vector: the same weights served by sentence-transformers
    # and by fastembed differ by 1.41e-03 max pairwise delta, which is 6x the
    # halfvec quantisation error. This string is written to
    # `title_embeddings.model_name`, so changing it invalidates every stored
    # vector through the stale predicate -- which is the fingerprint scheme
    # doing the work a migration would otherwise have to.
    embedding_model: str = Field(default="fastembed:BAAI/bge-small-en-v1.5", min_length=1)
    # Measured on CPU: best throughput at 16, flat from 16 to 64, degrading
    # at 128. `le=512` because the ceiling here is memory, and the cost of
    # being wrong is an OOM inside a worker pass rather than a slow one.
    embedding_batch_size: int = Field(default=16, ge=1, le=512)
    # Sets `HF_HUB_OFFLINE` before the model library is imported, and it is
    # not a hardening flag. Measured: with a warm cache, no network and the
    # variable unset, the load *fails* with `RuntimeError: Cannot send a
    # request, as the client has been closed` -- huggingface_hub reusing a
    # closed client on its retry path, in a message naming neither the
    # network nor the cache. Reproduced two independent ways. It is also the
    # only setting under which a genuine cache miss produces a
    # comprehensible `OSError`. An operator warming the cache for the first
    # time sets this false for that one run.
    #
    # **And that override is why `HF_HUB_OFFLINE` is deliberately not in
    # `compose.yml`'s `environment:` block**, which M6's plan asked for as a
    # topology fact. `environment:` beats `env_file:` absolutely, and this
    # setting reaches the library through `os.environ.setdefault`, so a
    # hard-set `HF_HUB_OFFLINE=1` in compose would make
    # `USHER_EMBEDDING_OFFLINE=false` dead config inside the container --
    # exactly the shape `USHER_COMPOSE_` exists to prevent -- and an operator
    # could never warm the cache there. The setting also reaches every entry
    # point compose cannot: `uv run usher work`, `usher index`, a dev shell.
    # (The shipped image installs no embedding extra and carries no model
    # cache, so the variable would be inert there in any case.)
    embedding_offline: bool = True

    # The LLM (PRD 06's curation, PRD 05's query expansion). Read by
    # `composition.llm_client`, which is the one place any of them is
    # touched.
    #
    # **Off by default, and that is the honest default twice over.** It is
    # the `embedding_enabled` argument -- a deployment with this off is
    # *narrowed*, since nine of ten row providers need no model at all -- and
    # it is also the only setting in this file whose "on" state sends the
    # household's data to a machine the household may not own. A default that
    # curated out of the box would make that a thing an operator discovers
    # rather than chooses.
    llm_enabled: bool = False
    # The provider abstraction, and the whole of it (ADR-0027). OpenAI,
    # OpenRouter, Together, Groq, DeepSeek, Mistral, vLLM, llama.cpp, Ollama
    # and LM Studio all serve `POST {base_url}/chat/completions`, which is
    # what makes `litellm` a dependency bought for wire formats reachable
    # through OpenRouter anyway. **The default is localhost** because this
    # project's reference deployment is self-hosted; a default naming a hosted
    # provider would be a default that sends a watch history off the box.
    llm_base_url: str = Field(default=DEFAULT_LLM_BASE_URL, min_length=1)
    # `SecretStr`, and `None` is a first-class value: a local vLLM or an
    # Ollama needs no credential, and sending `Bearer None` is how a client
    # fails against the deployment this is actually for.
    llm_api_key: SecretStr | None = None
    llm_model: str = Field(default="gpt-4o-mini", min_length=1)
    # The token ceiling on one completion. It is a correctness setting rather
    # than a cost one: measured, a completion that hits the ceiling under
    # guided decoding comes back as *valid* JSON with rows missing off the
    # end, so the adapter refuses `finish_reason == "length"` outright. Five
    # rows of eight cards with a reason each is ~500 tokens; 2048 is room to
    # be wrong without paying for a 32k answer nobody reads.
    llm_max_output_tokens: int = Field(default=2048, ge=256, le=32_768)
    # A generation is a background job with a whole backoff schedule behind
    # it, so a long timeout costs a worker pass rather than a request. 120 s
    # is roughly 20x the slowest measured completion (6.5 s, for the UUID arm
    # ADR-0028 rejects).
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    # Cost, in dollars per million tokens, because **no provider reports
    # cost** -- the live `usage` object carries token counts and nothing else,
    # which is why PRD 10's "litellm reports per-call cost natively" is
    # corrected. `Decimal`, never `float`: this number is summed over a month
    # and 1,200 in at $3/Mtok plus 340 out at $15/Mtok is exactly 0.0087.
    #
    # Both default to 0, which is the *honest* value for a local model and the
    # wrong one for a hosted model an operator forgot to price. That failure
    # is a cost dashboard reading zero, and the mitigation is that
    # `llm_calls.tokens_in`/`tokens_out` are recorded exactly, so spend is
    # recomputable from the ledger afterwards. A bundled price table would be
    # a third-party dataset in the repository.
    llm_price_in_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    llm_price_out_per_mtok: Decimal = Field(default=Decimal(0), ge=0)

    # How many candidates one generation's prompt carries. Read by
    # `composition.build_pipeline`, which hands it to `CandidatePoolService`.
    #
    # **A setting rather than a constant, and the sibling that settles it is
    # `llm_max_output_tokens` rather than `taste.py`'s constants.** Those are
    # not settings because `user_taste` is a *stored* artefact and a knob that
    # changes what "taste" means leaves every cached centroid computed under
    # the old meaning with nothing to tell them apart. Nothing here is stored:
    # a pool is assembled, sent, and discarded, and `curated_rows` is
    # regenerated nightly by a process no re-run reproduces anyway. What this
    # number is really about is the *context window of whatever model
    # `USHER_LLM_BASE_URL` names*, which is a deployment fact and not a
    # product one -- a 16k local model and a 200k hosted one have genuinely
    # different right answers, and an operator must be able to say so without
    # editing code.
    #
    # 200 is measured rather than round: ADR-0028's three handle arms all ran
    # against a 200-film pool, where the index spelling costs **2,924 prompt
    # tokens** -- so a candidate is ~14.6 tokens, and the same pool addressed
    # by UUID is 9,041, i.e. most of a 16k budget spent on identifiers.
    #
    # `le=1000` is that arithmetic: ~14.6 tokens a candidate puts 1,000 at
    # ~14.6k before the watch history or the instructions, which no 16k model
    # can answer. A context-length 400 is a permanent failure for that prompt
    # whose only fix is a smaller pool (trap 13), so the ceiling is a bound on
    # a misconfiguration that parks a job rather than degrading it. `ge=1`
    # rather than something friendlier because a pool of one is a legal,
    # useless configuration and this file does not invent product minima --
    # what a *row* needs is a card floor, which is the validator's
    # `DEFAULT_MIN_CARDS` and is deliberately **not** a setting: it crosses the
    # prompt, the request schema and the validator from one definition, and
    # `composition.build_curation_service` takes that default rather than
    # wiring a second value that can disagree with it. (This comment named a
    # `curation_min_cards` field until 2026-08-07; no such field was ever
    # shipped, and the sentence read as though one had been.)
    curation_pool_size: int = Field(default=200, ge=1, le=1000)

    # The retrieval half. Every one of these is read by
    # `composition.build_pipeline`, which constructs the two indexes and
    # `SearchService`.

    # The ceiling on `SearchRequest.limit`, applied by `SearchService` before a
    # request reaches an index -- not the default, the most a caller may ask
    # for. `le=200` because every candidate becomes a `SearchResult` assembled
    # in application code and RRF fuses two lists of this size; 10,000 is a
    # scan wearing a search's name.
    search_result_limit: int = Field(default=50, ge=1, le=200)
    # Reciprocal Rank Fusion's smoothing constant: `1 / (k + rank)`. It sets
    # how fast a hit's contribution decays with rank, so a small k makes rank 1
    # dominate and a large one flattens both lists into near-equal votes. 60 is
    # the value RRF's original paper uses and the one ADR-0002 assumes. `ge=1`
    # because k=0 makes the top rank's weight unbounded against the second's,
    # which is "return the first list". **Not the constant `SearchService`'s
    # relevance term uses**, which is 1: that term is scaled against a
    # popularity term in [0, 1) rather than against a second candidate list,
    # and sharing this number would make relevance two orders of magnitude
    # smaller than popularity.
    search_rrf_k: int = Field(default=60, ge=1, le=1000)
    # pgvector's `hnsw.ef_search`, set per statement rather than globally. The
    # GUC's own default is 40 and is not what this wants: measured, a filtered
    # query at 40 returned 0.88 rows of a requested 10. Larger is more accurate
    # and linearly slower. `ge=1` is pgvector's floor; `le=1000` because beyond
    # that the index is a scan with extra steps.
    search_hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    # `pg_trgm`'s `similarity()` floor for the suggest path. Bounded to (0, 1]
    # because that is `similarity()`'s own range: 0 admits every row in
    # `titles` as a candidate, which is the latency cliff PRD 05 says the
    # narrow path exists to avoid, and 1.0 admits only exact matches, which is
    # `LIKE`.
    search_trigram_threshold: float = Field(default=0.3, gt=0.0, le=1.0)
    # How many trigram candidates are collected before the `levenshtein`
    # re-rank. Measured: 1,774 candidates against a 300,000-row table is a 169x
    # reduction in `levenshtein` calls, and edit distance over the whole table
    # is the exact cliff ADR-0002 names. It must exceed `search_result_limit`
    # or the re-rank can only reorder what the cap already chose.
    search_suggest_candidates: int = Field(default=200, ge=1, le=2000)

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

    @field_validator("tmdb_api_key", "otlp_endpoint", "llm_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """An env var that is present but empty (as `.env.example` ships
        `USHER_TMDB_API_KEY=` and `OTEL_EXPORTER_OTLP_ENDPOINT=`) means
        "not set", not "set to the empty string" — keep `str | None` honest.

        **`llm_api_key` joined this list because the suite caught it**, and it
        is the one of the three where the empty string is not merely untidy:
        a local vLLM or Ollama is configured with no credential at all, so
        `USHER_LLM_API_KEY=` is the *documented* way to say so — and a
        `SecretStr("")` is truthy enough to build an `Authorization: Bearer `
        header, which a permissive server accepts and a strict one rejects
        with a 401 naming a credential the operator never set.
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
