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
#: one setting in this file whose *default* is a refusal rather than a limit.
#:
#: The gap-closer runs `reconcile(source, DELTA, adapter)`, and a DELTA reads
#: its `since` from the newest **completed** item-lane run. With none there is
#: no `since`, so the "delta" is `list_items(since=None)` -- the whole library,
#: 1,126,789 items on the household this project measures -- performed by
#: `uvicorn` on startup with no operator command. A gap is the window a socket
#: was down; with no completed walk the window is the entire catalog, which is
#: `usher sync`'s job.
#:
#: - ``cursored`` -- close the gap only when a completed walk gives a `since`.
#:   Shipped default. A deployment that has synced even once is unaffected.
#: - ``always`` -- walk when there is no cursor. Logged at WARNING before the
#:   walk starts, every time. ⚠️ **Not quite the pre-2026-08-19 behaviour any
#:   more**: the walk it starts still passes through
#:   ``USHER_PUSH_GAP_MAX_ITEMS`` (M10 S6), so at that setting's default it
#:   stops after 20,000 items and records ``FAILED``. Set the ceiling to ``0``
#:   as well for a genuinely unbounded gap close. The two settings answer two
#:   different halves of one hazard and compose rather than override.
#: - ``never`` -- no gap-closing walk at all. Costly and deliberate: Emby does
#:   not re-deliver what a disconnected client missed, so whatever changed
#:   during an outage waits for the operator's next walk.
PushGapClose = Literal["cursored", "always", "never"]


def settings_rejection(exc: ValidationError, *, entry_point: str) -> str:
    """pydantic's diagnosis with every rejected value stripped out.

    **This is a security control, not formatting**, and it lives here rather
    than beside its first caller because it has two. A pydantic v2
    `ValidationError` renders as

        ... [type=value_error, input_value='mysql://admin:hunter2@db/usher', ...]

    so a `USHER_DATABASE_URL` with the wrong driver prints the whole DSN and a
    short `USHER_SECRET_KEY` prints the key. Every credential in `Settings` is
    a `SecretStr` precisely so it cannot reach a log line; a `ValidationError`
    is the one path that renders the *input* rather than the field.

    **It was in `usher.cli` and `alembic` was leaking through the gap** --
    found 2026-08-13. `usher.db.migrations.env` calls `get_settings()` with no
    boundary of its own, so `uv run alembic upgrade head` with
    `USHER_DATABASE_URL` absent printed the raw traceback, `input_value={...}`
    and a truncated `secret_key` included. Two things made it worse than the
    original defect rather than a smaller copy of it. The CLI's version leaked
    a *rejected* value; this one leaked **every field pydantic echoes**, so a
    missing DSN exposed the secret key. And the container's `CMD` is
    `alembic upgrade head && exec python -m usher`, which makes that traceback
    the **first thing in the log** of a misconfigured deployment.

    An import-linter contract forbids anything importing `usher.cli`, so the
    repair could not be a call into it. This is the same collapse
    `services/llm_ledger.py` and `db/repositories/_errors.py` were: one
    definition, because two copies of a measured control are two chances to
    lose one.

    Same trade as `usher.api.errors` makes for a 422: `loc` and `msg` survive,
    so an operator still learns which setting was wrong and what it should have
    been, and the value never does. **`msg` is scrubbed as well as `input`
    dropped** -- no validator in `Settings` interpolates the value into its own
    message today and none of pydantic's built-ins do, so the scrub exists so
    that writing one does not quietly reopen this.
    """
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

    # The connection pool, settings-driven since M9's W1 and hardcoded before
    # it. `usher.db.base.build_engine`'s own comment predicted this task:
    # *"Revisit if/when a milestone adds a second long-running process (e.g. a
    # worker pool) sharing this pool."* It is not a second process -- it is
    # `job_concurrency` jobs, each holding a session, **plus the claim and the
    # heartbeat, plus the API's own requests**, because `usher serve` runs the
    # worker lane inside the same process against the same engine.
    #
    # The default budget at the shipped `job_concurrency = 12`: 12 jobs + 1
    # claim + 1 heartbeat = 14 for the worker, leaving 6 of `db_pool_size` and
    # all 10 of the overflow for the API, the push lanes and the rows.refresh
    # lane. The old 10/5 could not hold the worker alone.
    db_pool_size: int = Field(default=20, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)

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
    # The proactive outbound ceiling: calls to one source are spaced at least
    # `1/rate` seconds apart (ADR-0039). `0` is unlimited -- the `ge=0` shape
    # `push_gap_min_interval_seconds` uses, not the `ge=1` a size takes --
    # because "off" is a value an operator sets. The gate is owned by a
    # `SourceGateRegistry` built once at each composition root and keyed by
    # `source.id`, so this is **one gate per source per process** -- the push
    # lane, the worker lane and every request in a server process all pace
    # against the same one (ADR-0039 s4). A second process is a second
    # registry, so two `usher work` containers against one server spend
    # `2 x rate`: a capacity decision an operator makes, exactly as
    # `USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` already are.
    #
    # The default is derived from S1, not chosen. S1 measured (2026-08-15,
    # `.claude/rules/emby-push-and-ingest.md`, one household one evening) a
    # 200-item page at **mean 6.0369 s / p95 9.1713 s** and a single-item read
    # at median 0.1495 s. Little's law over a page paced by the Emby-facing
    # `KIND_CONCURRENCY` of **4** gives two figures: the mean-based expected
    # concurrent-walk rate `4/6.0369 = 0.66` rps (a sum over pages wants the
    # mean) and the conservative p95 ceiling `4/9.1713 = 0.436` rps. **0.4** is
    # below both, a courtesy margin under either.
    #
    # It is inert on a sequential walk (`1/6.0369 = 0.17` rps) and binds
    # single-item reads (~27 rps four-in-flight at S1's 0.15 s). Whether it
    # binds a *concurrent* walk is unmeasured and is **S7's** to settle -- under
    # S1's ~0.68 rps a 0.4 gate would bind it, not sit "just under" it -- since
    # every S1 request was sequential. S1's table is the evidence, not a guess.
    source_requests_per_second: float = Field(default=0.4, ge=0)

    # The ingest pipeline (PRD 03). Same reasoning as the bulk and source
    # settings above: PRD 08's TOML config layer does not exist yet.
    sync_batch_size: int = Field(default=1_000, ge=1, le=50_000)
    # The fraction of a source's items one reconcile may mark unavailable
    # before it refuses and changes nothing (ADR-0015). 1.0 disables the
    # guard, which is what an operator deliberately removing a library
    # passes on the command line.
    #
    # **0.25 stayed on 2026-08-19 (M10 S9), and it stayed because somebody
    # looked.** A number that stayed because nobody looked and a number that
    # stayed because it was measured are the same value and different claims,
    # so this comment says which. Issue #20 asked whether the default suits a
    # library the operator does **not own** and asked for a reading across a
    # churn event nobody can schedule. What S8 found instead, in the
    # deployment's own `sync_runs`: the ceiling had **already fired**, on
    # 2026-08-13, refusing 60 of 180 items at 33% -- and nothing had been
    # deleted. The walk was *bounded* and saw 120 of the 180 Usher held.
    #
    # So the one observation of this guard firing in the field was **Usher's
    # own partial coverage, not the source's churn**, and moving a ceiling on
    # that evidence would be tuning it against the wrong population. The
    # drift probe (`scripts/measure_source_drift.py`) cannot supply the right
    # one here either: it read 11,851 available against a live 1,137,502, and
    # its lower bound is structurally 0 for any catalogue that lags its
    # source. Revisit when a **completed** full walk of a shared library
    # exists to measure -- none ever has.
    #
    # The per-source override is the post-v1 candidate, named rather than
    # taken: a `sources.max_retract_fraction real` column, nullable, falling
    # back to this. It is out of scope here because `Source` carries ten
    # fields and not one of them is tuning, so the first one is a data-model
    # decision and a migration rather than a default being chosen.
    sync_max_retract_fraction: float = Field(default=0.25, ge=0.0, le=1.0)
    job_batch_size: int = Field(default=20, ge=1, le=500)
    # How many jobs one worker process may have in flight at once, and the
    # per-kind ceiling for the network-bound kinds. `usher.services.jobs.
    # KIND_CONCURRENCY` holds the per-kind table and the measurement behind
    # every entry in it; this is the global bound and the value the entries
    # spelled `None` resolve to.
    #
    # **12 is Little's law over what M9's S3 measured, not a round number.**
    # p95 HTTP against TMDb was 0.4267 s over 130,334 requests, and ~0.033 s
    # of Postgres bookkeeping per job (S2's one-worker 10.38 rps against its
    # own 0.0637 s mean HTTP) makes a p95 job ~0.46 s -- so holding ADR-0005's
    # ~25 rps through the tail takes ~11.5 in flight. Below that the
    # *architecture* is the ceiling again, which is the defect W1 exists to
    # remove: S3's three workers reached 19.76 rps with a 10 rps-per-process
    # bucket that never once bound.
    #
    # `le=64` rather than unbounded, and the bound is the connection pool
    # rather than taste: every job in flight holds a session, so a value above
    # `db_pool_size + db_max_overflow` cannot run and would wait 30 s per job
    # on `pool_timeout` before failing. The validator below refuses that
    # combination outright instead of letting it be discovered in production.
    job_concurrency: int = Field(default=12, ge=1, le=64)
    # How long a claim may go un-heartbeated before another worker may take it
    # back. Paired with `JobQueue.touch`, which `JobWorker` calls every
    # `job_lease_seconds / 3` for everything in flight -- so this is a bound on
    # *"the process stopped"*, not on how long a job may take, and a `bootstrap`
    # phase running for hours is not at risk. `ge=10` because a lease shorter
    # than a couple of heartbeat intervals plus a slow database is a worker
    # that recovers its own live claims.
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
    #
    # **Two runtimes since `m09e`, and the prefix is what picks one.**
    # `fastembed:<checkpoint>` loads the model in-process; `openai:<checkpoint>`
    # calls `POST {embedding_base_url}/embeddings` on an OpenAI-compatible
    # server. `composition._load_embedder` dispatches on it, and an unknown
    # prefix is an error rather than a silent fallback -- a typo that fell back
    # would embed 1.27M titles with the wrong model and record the right name.
    #
    # **The default moved off `bge-small-en-v1.5`, and not because anything is
    # wrong with it.** `m09e` widened the column to 1024 for `BAAI/bge-m3`, and
    # `EMBEDDING_DIMENSIONS` is deployment-wide rather than per-model, so a
    # 384-wide checkpoint can no longer be stored at all. Of the 1024-wide
    # models `fastembed` 0.8.0 actually ships -- enumerated, not assumed --
    # `BAAI/bge-large-en-v1.5` is the only well-measured English one, so it is
    # what a deployment with no inference server gets. It is **1.2 GB against
    # bge-small's 0.07**, which is a real regression in the service-free
    # install and is the price of the width, paid here rather than hidden.
    #
    # **`fastembed` cannot serve `bge-m3` at all**, which is why the second
    # runtime exists rather than being a preference: enumerating
    # `TextEmbedding`, `SparseTextEmbedding`, `LateInteractionTextEmbedding`,
    # `ImageEmbedding` and `LateInteractionMultimodalEmbedding` on
    # `fastembed` 0.8.0 returns no `bge-m3` in any of the five (2026-08-13).
    embedding_model: str = Field(default="fastembed:BAAI/bge-large-en-v1.5", min_length=1)
    # Measured on CPU: best throughput at 16, flat from 16 to 64, degrading
    # at 128. `le=512` because the ceiling here is memory, and the cost of
    # being wrong is an OOM inside a worker pass rather than a slow one.
    embedding_batch_size: int = Field(default=16, ge=1, le=512)
    # Read only by the `openai:` runtime, and deliberately **not** reusing
    # `llm_base_url`. They are one endpoint on many hosted providers and two
    # processes here -- vLLM serves one model per process, so this deployment
    # runs `gemma-4-26b-a4b` on :8000 and `bge-m3` on :8001. Collapsing them
    # would make "point the embedder somewhere else" impossible without moving
    # curation too.
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
    # `composition.llm_client`, which is the one place all but one of them is
    # touched -- the exception is the model name, which is also read by
    # `build_curation_service` and `build_pipeline` so that `llm_calls.model`
    # records the string the client was built with on the path where no
    # response came back to read one from.
    #
    # **Off by default, and that is the honest default twice over.** It is
    # the `embedding_enabled` argument -- a deployment with this off is
    # *narrowed*, since nine of ten row providers need no model at all -- and
    # it is also the only setting in this file whose "on" state sends the
    # household's data to a machine the household may not own. A default that
    # curated out of the box would make that a thing an operator discovers
    # rather than chooses.
    #
    # **Two spenders, and since 2026-08-07 two switches.** Curation buys one
    # completion per household per generation; query expansion buys one per
    # semantic or fused search. This field is the *client*: with it false there
    # is no `LLMClient` anywhere in the process and neither spender exists.
    # `query_expansion_enabled` below is the second half.
    #
    # This block argued until 2026-08-07 that a second setting's only honest
    # default is "follow the switch above". **That argument is replaced by a
    # measurement rather than deleted**, because it was sound only while
    # expansion was believed to help: measured against a local
    # `gemma-4-26b-a4b`, expansion moved MRR 0.733 -> 0.373 and recall@10
    # 0.800 -> 0.533 over five mood queries and 150 real overviews. Two
    # features with opposite expected values cannot share a switch, and
    # `llm_calls` grouped by `purpose` -- the old argument's answer -- reports
    # what expansion cost *after* it has been paid.
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
    #
    # ✅ **The guard fired live for the first time on 2026-08-07, and its real
    # justification is stronger than the sentence above.** An unsatisfiable
    # *value* bound -- `maximum: 5` in the schema against a prompt asking for
    # numbers 1-200 -- made guided decoding **loop**: `1,2,3,4,3,1,2,3,4...`
    # for the entire 2,048-token budget, `finish_reason == "length"`, and the
    # adapter refused it. So the guard is not only about *"rows missing off
    # the end"*; it is what stops a **degenerate loop being read as a valid
    # answer**, which is the failure that would otherwise have arrived as a
    # well-formed row of repeated handles. One model, one evening,
    # `gemma-4-26b-a4b`.
    #
    # 🔶 **This setting and `curation_pool_size` below spend one budget and
    # nothing couples them.** The endpoint's real constraint is
    # `prompt_tokens + llm_max_output_tokens <= max_model_len`, so raising
    # this silently lowers the workable pool -- and the failure is a 400 that
    # parks the job rather than a warning at startup. Recorded rather than
    # solved: see `curation_pool_size`'s measured ceiling below.
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
    # tokens** for the probe prompt, and the same pool addressed by UUID is
    # 9,041 -- most of a 16k budget spent on identifiers.
    #
    # ⚠️ **The per-candidate figure that arithmetic gave is wrong for the
    # shipped prompt, re-measured 2026-08-07.** This comment read *"so a
    # candidate is ~14.6 tokens"* -- a *total* divided by a count, from a probe
    # prompt whose candidate line was name and year. Measured against the
    # prompt that ships, at four pool sizes, the **marginal** cost is
    # **20.40 tokens/candidate** (8 -> 200) and **20.45** (200 -> 600): +40%,
    # and the difference is the genre list `curation_prompt._genres` renders.
    # The whole prompt is 4,304 cold at pool 200 and 4,359 with three history
    # lines, against the probe's 2,924.
    #
    # 🔴 **And `le=1000` is a bound this milestone's own reference endpoint
    # cannot serve.** Measured 2026-08-07 against the local vLLM
    # (`gemma-4-26b-a4b`, `max_model_len` 16,384) with the shipped defaults:
    # **1,000 -> HTTP 400. 700 -> HTTP 400. 600 -> works**, at 12,540 prompt
    # tokens. The constraint is not the context window alone, it is
    # `prompt_tokens + llm_max_output_tokens <= max_model_len` -- and
    # **nothing in this file couples the two**, so raising
    # `USHER_LLM_MAX_OUTPUT_TOKENS` silently lowers the workable pool.
    # `le=1000` is kept rather than lowered to 600, because 600 is *this*
    # endpoint's answer and the whole argument above is that the right number
    # is a deployment fact -- a 200k-context hosted model has a different one.
    # What the ceiling is honestly a bound on is arithmetic no configuration
    # can satisfy anywhere, and the mechanism below is what makes the rest
    # survivable: a context-length 400 is a permanent failure for that prompt
    # whose only fix is a smaller pool (trap 13), the adapter translates it to
    # `PortDataMalformed`, and `JobWorker` parks immediately rather than
    # spending four more completions on the same wall. Verified live: the
    # 400 arrived, was translated, and parked. `ge=1`
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

    # PRD 05's query expansion: one completion rewriting the query before
    # `SearchService` embeds it. Read by `composition.build_pipeline`, which
    # builds the `QueryExpansionService` or does not, and by `cli._search`,
    # which uses it to decide whether opening an `httpx.AsyncClient` for the
    # command would buy anything.
    #
    # **Named for the feature rather than for the client or for the lane**, the
    # call `curation_pool_size` already made one block up: `llm_*` is the
    # endpoint and its credential, `search_*` is the retrieval tuning
    # `build_pipeline` hands to the two indexes, and this is neither -- it is a
    # switch on one LLM *feature*, which is what `curation_*` is too.
    #
    # **Off by default, and that default is a measurement rather than caution.**
    # PRD 05 has named query expansion the cheaper, better-evidenced lever for
    # mood queries since M1, on the literature's authority. Run on 2026-08-07
    # against a local `gemma-4-26b-a4b` -- five mood queries against the 150
    # most-voted catalog titles' real overviews, embedded with the shipped
    # `compose_document` and the shipped `FastEmbedEmbedder`, targets written
    # down before any cosine was computed -- it made retrieval **worse**: MRR
    # 0.733 -> 0.373, recall@10 0.800 -> 0.533, with the typed query winning
    # four of the five queries and tying the fifth. A label-free control says
    # why: pairwise cosine *between the five queries themselves* rose from
    # 0.5417 to 0.5975 mean and 0.6328 to 0.7784 max, so five distinct searches
    # came back more alike than they went in. See
    # `docs/prd/05-search-and-similarity.md` for the caveats, which are real --
    # one model, one 150-document corpus, five queries.
    #
    # **`true` here with `llm_enabled` false is refused rather than ignored**
    # -- see `_query_expansion_needs_a_client` below.
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
    #
    # **200 since 2026-08-19, and it is the first value of this constant with a
    # recall figure from a real index under it** (issue #32). 132,409 real
    # 1024-lane `bge-m3` vectors, 12 typed plot queries, recall@10 against an
    # exact scan over the whole embedded population, 120 gold slots per
    # condition, `relaxed_order` on, unfiltered:
    #
    #   ef  40 -> 0.700   p50  3.21 ms   p95  4.75 ms
    #   ef 100 -> 0.858   p50  4.77 ms   p95  7.30 ms   <- the old default
    #   ef 200 -> 0.917   p50 10.59 ms   p95 16.18 ms   <- ships
    #   ef 400 -> 0.967   p50 20.13 ms   p95 29.90 ms
    #   ef 1000 -> 0.992  p50 45.46 ms   p95 67.50 ms
    #
    # 400 and 1000 buy more and are refused on cost, not on recall: the
    # recorded query-side budget is a p50 of 5.7 ms for the embed, and a scan
    # whose p50 is 20 ms makes the vector half of a search four times the
    # model's. 200 doubles the scan and keeps the pair inside ~16 ms at p50.
    # Under a 4.8%-selectivity genre filter the same sweep is 0.783 -> 0.808,
    # so this is not a filtered-path fix and does not pretend to be one; see
    # `.claude/rules/search-and-embeddings.md`, which records what over-fetch
    # and re-rank does there instead.
    search_hnsw_ef_search: int = Field(default=200, ge=1, le=1000)
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
    # `LibraryChanged` during a library scan and it can name thousands; at
    # `get_item`'s measured 0.1649 s mean (M10 S1, 2026-08-15 -- see
    # `.claude/rules/emby-push-and-ingest.md`) a thousand named items is
    # nearly three minutes of serial upstream, so a request per changed item
    # is a design defect rather than a slow path. Bounded above at 500,
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
    # What the gap-closer may do when the delta has no cursor -- see
    # `PushGapClose` above for the vocabulary and the measurement. **The one
    # default in this block that changes behaviour for an existing
    # deployment**, and only for one that has never completed an item walk:
    # such a deployment used to have its whole library walked by the push lane
    # on startup, and now gets a WARNING naming the source and pointing at
    # `usher sync` instead. Everything past its first walk has a `since` and
    # behaves exactly as before. The rate limit beside it is not a substitute:
    # `push_gap_min_interval_seconds` bounds how *often* the walk happens and
    # says nothing about how large it is.
    push_gap_close: PushGapClose = "cursored"
    # The ceiling on **one** gap-closing delta, counted in items, and the
    # other half of the same hazard (M10 S6). `push_gap_close` above answers
    # the delta with *no* cursor; what is left is a delta *with* a cursor
    # that is still large -- a source Usher has not reached for a month, a
    # library the owner re-scanned, or a `deferred_to_delta` outcome arriving
    # on a busy evening.
    #
    # **Items, deliberately, and not pages.** `MAX_PAGES`
    # (`adapters/emby/adapter.py`) is already the dead-man's switch against a
    # server that ignores `StartIndex`, and exhausting it raises
    # `PortDataMalformed` -- so a ceiling spelled in pages would report a
    # deliberate, correct stop as a broken upstream in the one message an
    # operator acts on.
    #
    # **The default is derived, not picked.** PRD 03 measured this
    # household's 30-day item-lane delta at **28,934 items**
    # (`MinDateLastSaved`), i.e. 145 pages at `source_page_size`'s 200, and
    # M10 S1 measured a page at a **6.0369 s** pooled mean (2026-08-15;
    # `.claude/rules/emby-push-and-ingest.md`) -- the mean rather than the
    # median because a sum over pages wants the mean of a right-tailed
    # distribution. That is ~14.6 minutes of upstream before the lane has
    # done anything else, issued by a bare `uvicorn` against every enabled
    # source at once. 20,000 items is 100 pages, **~10 minutes** at that
    # mean, and roughly three weeks of the same household's measured change
    # rate. Deliberately *under* 28,934: a ceiling the one measured
    # pathological case slips beneath is not a ceiling. One household, one
    # evening -- re-derive it against your own before trusting it.
    #
    # The trade, in one sentence: **a ceiling buys a bounded startup and
    # costs a reconcile an operator must run**, because a walk stopped here
    # records `FAILED`, advances no cursor, and leaves the rest of the
    # window for `usher sync --kind full` (`services/reconcile.py`).
    #
    # `ge=0` with zero meaning unlimited -- the same deliberate exception
    # `push_gap_min_interval_seconds` above makes, and for the same reason:
    # "close the whole gap however big it is" is expensive and correct,
    # unlike every other zero in this block.
    push_gap_max_items: int = Field(default=20_000, ge=0)
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

    # The image proxy (PRD 07's `GET /images/{id}`, M9). Four settings, and
    # **the ladder is deliberately not one of them**
    # ([ADR-0032](../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)):
    # `IMAGE_LADDER` is a tuple in `usher.ports.images`, because the four
    # widths are what the *cache* is bounded by and a knob nothing reads is
    # dead config wearing a control's name. PRD 08's Configuration table said
    # otherwise until 2026-08-11 and is corrected.
    #
    # Same reasoning as every block above for why these four are environment
    # settings: PRD 08's TOML config layer does not exist yet. All four are
    # read by `composition.image_proxy`.
    #
    # Where the bytes live. The dev default sits beside `bulk_data_dir`'s
    # `data/bulk` and inside `.gitignore`'s `data/`; the container's is
    # `/data/images`, which `compose.yml` bind-mounts and which is therefore
    # the one image setting `environment:` owns rather than the operator --
    # a bind-mount path is a topology fact, and a relative path inside a
    # container whose `WORKDIR` is `/app` would put the cache in the image.
    image_cache_dir: Path = Path("data/images")
    # The most one CDN answer may be, enforced **while it streams** rather
    # than against a `Content-Length` the sender controls. 5 MiB is above
    # every byte this proxy can legitimately receive and still bounds a lying
    # upstream: ADR-0032 measured the largest artwork anywhere in its samples
    # at 4,731,805 bytes, and that is an `original`, which the ladder cannot
    # express and the fetcher never requests -- the largest *rung* measured is
    # a 563 KB median poster at `w1280`. `ge=1` rather than a friendlier floor
    # for the reason `curation_pool_size` has one: a one-byte ceiling is a
    # legal, useless configuration and this file does not invent product
    # minima.
    image_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    # How long one CDN fetch may take. An order of magnitude below
    # `llm_timeout_seconds`' 120 and it is the *lane* that decides it, not the
    # upstream: a curation completion is a worker job whose long timeout costs
    # a worker pass, and this is a request with a person waiting at the other
    # end of it. A cold image that has not arrived in ten seconds is better
    # reported than waited for, because the client will ask again.
    image_fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    # The provider's image host, `{base}` in the ladder's `{base}{rung}{path}`.
    #
    # **A configured constant rather than a `/configuration` call**, and the
    # cost being avoided is on the request path: resolving `secure_base_url`
    # per cold image is a second network round trip, against an authenticated
    # endpoint, for a value that changes approximately never. The default was
    # read live from that endpoint on 2026-08-11 (`secure_base_url`). A
    # setting rather than a module constant for the reason `tmdb_base_url` is
    # one: a household behind a restrictive network puts a caching proxy in
    # front of it.
    #
    # **The literal lives here and nowhere else.** `ProviderCdnImageFetcher`
    # takes the base URL as a required argument rather than defaulting to a
    # constant of its own, so there is one definition of the measured host and
    # `.env.example` documents it — `config.py` imports nothing from `usher`
    # and this setting is not the thing to change that for.
    image_cdn_base_url: str = Field(default="https://image.tmdb.org/t/p/", min_length=1)

    # Whether this process also serves Usher Console at `/console` (see
    # `usher.api.console`). On by default because a self-hosted product whose
    # only interface is `curl` is not one, and because the console is built
    # into the image rather than fetched — turning it off saves nothing at
    # runtime but a `StaticFiles` mount.
    #
    # **Off is a real deployment, not a debugging aid.** A worker-only or
    # push-only container (`USHER_WORKER_ENABLED` / `USHER_PUSH_ENABLED`) has
    # no browser pointed at it, and a household that puts its own client in
    # front of this API does not want a second one answering `/`.
    console_enabled: bool = True
    # Where the built bundle lives, relative to the working directory exactly
    # as `image_cache_dir` is.
    #
    # **`web/dist` in both places on purpose.** It is where `vite build` writes
    # in a checkout, and the Dockerfile copies the `console` stage's output to
    # `/app/web/dist` so the container agrees rather than needing an override —
    # which is the difference between a setting an operator never touches and
    # one `.env.example` has to explain. `image_cache_dir` is the counterexample
    # and it earns its divergence: that path is a bind mount, so it is a
    # topology fact. This one is just where a build lands.
    #
    # A missing bundle is **not** a configuration error: `mount_console` logs
    # and serves the API alone. A backend running from a checkout with no
    # `npm run build` has to boot, or `uv run pytest` would need node.
    console_dist_dir: Path = Path("web/dist")
    # The deployment's Grafana, for the Insights screen's "Open in Grafana".
    #
    # `None` by default and **rendered as absent rather than as a dead link**
    # when unset, which is the same distinction the rest of this product makes
    # between never computed and computed and empty. There is no default value
    # to guess: PRD 10's stack is a sibling compose project, its host is a
    # deployment fact, and a wrong guess is worse than a stated absence.
    #
    # Deliberately **not** proxied through this app. The design forbids
    # iframing Grafana (its own frame-ancestors policy would refuse anyway),
    # so this is a link the browser follows directly and Usher never sees.
    grafana_url: str | None = None
    # The deployment's Tempo, for the "Open trace" link on a rendered problem
    # document. Same rules as `grafana_url`: unset means the link is absent,
    # and this app never proxies it.
    #
    # That one link is what PRD 10's telemetry is *for* on a failure path —
    # every response already carries a trace id, and without somewhere to open
    # it the id is a string an operator cannot act on.
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
        """The one combination of the two LLM switches that cannot mean
        anything, refused at startup rather than left to mean nothing.

        Query expansion is a completion in front of an embed. With
        `llm_enabled` false there is no `LLMClient` in the process at all --
        `composition.llm_client` answers `(None, no-op)` and `build_pipeline`
        has nothing to build a `QueryExpansionService` from -- so
        `USHER_QUERY_EXPANSION_ENABLED=true` beside it is a knob an operator
        turned with no effect. That is the failure `extra="forbid"` and
        `USHER_COMPOSE_` both exist to prevent, arriving as a *state* rather
        than as a typo, and this project has already paid for it once:
        `USHER_WORKER_ENABLED` was documented, worked when delivered directly,
        and was silently ignored where the docs pointed.

        The other three combinations are all reachable and all meaningful, so
        this refusal is the whole of the coupling between the two fields:
        off/off is the shipped default, on/off is an LLM deployment that
        curates and embeds every query as typed, and on/on adds the rewrite.

        **The message names both variables**, which is not cosmetic: an
        operator who kills spend by setting `USHER_LLM_ENABLED=false` meets
        this on the next start, and a sentence naming only the field that was
        set would send them to delete the line they meant to keep rather than
        to the line that makes it work.

        A cross-field rule for `_suggest_cap_leaves_room_to_choose`'s reason:
        neither field can express it alone, and a bound that is a real
        constraint belongs in the type system wherever it fits.
        """
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
