/**
 * Usher's settings, as the Configuration screen renders them.
 *
 * **This is a catalogue, not a cache.** Every row carries the setting's name,
 * its subsystem, its *default* and what it controls — all of which are
 * properties of the software and knowable without asking the deployment. What
 * none of them carries is a current value, because no route returns one: see
 * `Config.tsx`'s REQUIRES BACKEND WORK block. A row gets an `observed` only
 * where a real read proves it, and the proof travels with the value.
 *
 * **No row carries a secret's value, and there is no field it could be put
 * in.** `secret: true` is the whole of what this file knows about the five
 * `SecretStr` settings, which is what makes "a secret is never rendered"
 * a property of the data rather than a rule the renderer has to remember.
 *
 * `measured: true` marks a default that is a *measurement* rather than a
 * preference — `search_hnsw_ef_search`'s recall sweep, `query_expansion`'s MRR
 * regression, `job_concurrency`'s Little's-law budget. The measurement itself
 * is in `about`, so the screen carries the explanation where one exists rather
 * than printing a bare number.
 *
 * All but two are read under the `USHER_` prefix; `otlp_endpoint` and
 * `service_name` are read under OpenTelemetry's own variable names, so they
 * appear here spelled the way the environment must spell them.
 *
 * The count is `SETTING_COUNT` at the bottom of this file and is derived from
 * the list. Do not write it down anywhere.
 */

export type SettingRow = {
  /** The environment variable's name, exactly as it must be spelled. */
  key: string
  group: string
  /** The field default, or `required` where the field has none. */
  def: string
  about: string
  /** A `SecretStr` field. No value for one exists anywhere in this module. */
  secret: boolean
  /** The default is a measurement; `about` carries it. */
  measured: boolean
  /**
   * What a real read proved about this deployment, and the read that proved it.
   * Absent on every row nothing serves, which is nearly all of them.
   */
  observed?: { value: string; proof: string }
}

export const CONFIG: readonly SettingRow[] = [
  /* ------------------------------------------------------------- database */
  {
    key: 'USHER_DATABASE_URL',
    group: 'database',
    def: 'required',
    about: 'Async DSN. Must carry the postgresql+asyncpg driver; changing it is a restart.',
    secret: true,
    measured: false,
  },
  {
    key: 'USHER_DB_POOL_SIZE',
    group: 'database',
    def: '20',
    about:
      'Connections per process. 20 because the worker lane runs in the same process and holds one session per job in flight: 12 jobs plus a claim and a heartbeat is 14, leaving 6 for the API.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_DB_MAX_OVERFLOW',
    group: 'database',
    def: '10',
    about: 'Extra connections above the pool before a checkout waits on pool_timeout.',
    secret: false,
    measured: false,
  },

  /* --------------------------------------------------------------- server */
  {
    key: 'USHER_HOST',
    group: 'server',
    def: '0.0.0.0',
    about: 'Bind address. Bind-all is the container default and what compose assumes.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PORT',
    group: 'server',
    def: '8000',
    about: 'Listen port inside the container. The published host port is a compose variable.',
    secret: false,
    measured: false,
  },

  /* -------------------------------------------------------------- logging */
  {
    key: 'USHER_LOG_LEVEL',
    group: 'logging',
    def: 'INFO',
    about: 'DEBUG, INFO, WARNING, ERROR or CRITICAL. A value outside the five is a startup failure.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_LOG_JSON',
    group: 'logging',
    def: 'true',
    about: 'Structured log records. Credentials never appear in one, including on error paths.',
    secret: false,
    measured: false,
  },

  /* ------------------------------------------------------------- security */
  {
    key: 'USHER_SECRET_KEY',
    group: 'security',
    def: 'required',
    about:
      'Encrypts stored source credentials. It has a minimum length and no default, so the process refuses to start without one.',
    secret: true,
    measured: false,
  },

  /* ------------------------------------------------------------ bootstrap */
  {
    key: 'USHER_BULK_DATA_DIR',
    group: 'bootstrap',
    def: 'data/bulk',
    about: 'Where downloaded IMDb, TMDb and MovieLens dumps are kept between resumable phases.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_BULK_BATCH_SIZE',
    group: 'bootstrap',
    def: '50000',
    about: 'Rows per committed batch during a bulk import. Each commit moves the resume cursor.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_WIKIDATA_ENDPOINT',
    group: 'bootstrap',
    def: 'https://query.wikidata.org/sparql',
    about: 'The crosswalk phase’s SPARQL endpoint. A setting because WDQS has documented mirrors.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_BULK_USER_AGENT',
    group: 'bootstrap',
    def: 'Usher/0.1 (+https://github.com/anirudhlath/usher)',
    about:
      'WDQS requires a descriptive agent naming the tool and a way to reach its operator. Add your own contact if you run at scale.',
    secret: false,
    measured: false,
  },

  /* -------------------------------------------------------------- sources */
  {
    key: 'USHER_SOURCE_PAGE_SIZE',
    group: 'sources',
    def: '200',
    about: 'Items per page when walking a source’s library.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SOURCE_TIMEOUT_SECONDS',
    group: 'sources',
    def: '30.0',
    about: 'How long one request to a media server may take before it is a failure.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SOURCE_REQUESTS_PER_SECOND',
    group: 'sources',
    def: '0.4',
    about:
      'The proactive outbound gate: one request per source per process, spaced 1/rate apart, never a burst. Zero is unlimited. The default is derived rather than picked — a household media server is a machine somebody is watching something on, and 0.4 rps is a courtesy margin under the rate this project measured against a real Emby 4.9.5.0.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_SOURCE_REAUTH_COOLDOWN_SECONDS',
    group: 'sources',
    def: '60.0',
    about:
      'How long a rejected credential is remembered. Without it, a wrong password turns every request into two.',
    secret: false,
    measured: false,
  },

  /* --------------------------------------------------------------- ingest */
  {
    key: 'USHER_SYNC_BATCH_SIZE',
    group: 'ingest',
    def: '1000',
    about: 'Items per committed batch during a sync walk.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SYNC_MAX_RETRACT_FRACTION',
    group: 'ingest',
    def: '0.25',
    about:
      'The share of a source’s items one reconcile may mark unavailable before it refuses and changes nothing. 1.0 disables the guard, which is what removing a library deliberately passes.',
    secret: false,
    measured: false,
  },

  /* ----------------------------------------------------------------- jobs */
  {
    key: 'USHER_JOB_BATCH_SIZE',
    group: 'jobs',
    def: '20',
    about: 'Jobs claimed per worker pass.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_JOB_CONCURRENCY',
    group: 'jobs',
    def: '12',
    about:
      'Jobs in flight per process. 12 is Little’s law over a measured p95 of 0.4267 s against TMDb across 130,334 requests, not a round number — holding ~25 rps through the tail takes ~11.5 in flight.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_JOB_LEASE_SECONDS',
    group: 'jobs',
    def: '300.0',
    about:
      'How long a claim may go un-heartbeated before another worker may take it back. A bound on "the process stopped", not on how long a job may take.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_JOB_MAX_ATTEMPTS',
    group: 'jobs',
    def: '5',
    about: 'After this many failures a job is parked with its error until an operator releases it.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_JOB_BACKOFF_SECONDS',
    group: 'jobs',
    def: '30.0',
    about: 'The base of the retry backoff, before jitter. Zero would collapse it to a hot loop.',
    secret: false,
    measured: false,
  },

  /* ---------------------------------------------------- metadata provider */
  {
    key: 'USHER_TMDB_API_KEY',
    group: 'metadata provider',
    def: 'unset',
    about: 'Your own key. Usher ships importers, never data. With none, enrich jobs are not claimed.',
    secret: true,
    measured: false,
  },
  {
    key: 'USHER_TMDB_BASE_URL',
    group: 'metadata provider',
    def: 'https://api.themoviedb.org/3',
    about: 'A setting because TMDb documents alternate hosts and a caching proxy is a normal deployment.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_TMDB_REQUESTS_PER_SECOND',
    group: 'metadata provider',
    def: '30.0',
    about:
      'TMDb publishes no number and documents "somewhere in the 40 requests per second range". 30 leaves headroom for the retry a 429 triggers.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_TMDB_REGION',
    group: 'metadata provider',
    def: 'US',
    about:
      'Which certification body’s rating lands in content_rating. Showing a household somebody else’s rating is worse than showing none.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_ENRICH_CACHE_MAX_AGE_DAYS',
    group: 'metadata provider',
    def: '30',
    about:
      'How long a cached provider payload is reused. Capped at 180 because TMDb’s licensing term is a six-month ceiling, expressed here as a type.',
    secret: false,
    measured: false,
  },

  /* ----------------------------------------------------------- embeddings */
  {
    key: 'USHER_EMBEDDING_ENABLED',
    group: 'embeddings',
    def: 'false',
    about:
      'Off by default and that is the honest default: the dependency lives behind an extra, and full-text plus trigram over all 1.27M titles needs no model at all. A deployment with this off is narrowed, not broken.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_EMBEDDING_MODEL',
    group: 'embeddings',
    def: 'fastembed:BAAI/bge-large-en-v1.5',
    about:
      'Runtime prefix plus checkpoint. The default moved off bge-small when the column widened to 1024 lanes: 1.2 GB against bge-small’s 0.07, which is the price of the width paid here rather than hidden.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_EMBEDDING_BATCH_SIZE',
    group: 'embeddings',
    def: '16',
    about:
      'Measured on CPU: best throughput at 16, flat from 16 to 64, degrading at 128. The ceiling is memory.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_EMBEDDING_BASE_URL',
    group: 'embeddings',
    def: 'http://localhost:8001/v1',
    about:
      'Read only by the openai: runtime, and deliberately not shared with the LLM endpoint — vLLM serves one model per process.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_EMBEDDING_API_KEY',
    group: 'embeddings',
    def: 'empty',
    about:
      'Empty means no Authorization header at all rather than an empty bearer token, which is what a local server wants.',
    secret: true,
    measured: false,
  },
  {
    key: 'USHER_EMBEDDING_TIMEOUT_SECONDS',
    group: 'embeddings',
    def: '30.0',
    about: 'One request carries a whole batch, and a cold model behind a proxy can take seconds.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_EMBEDDING_OFFLINE',
    group: 'embeddings',
    def: 'true',
    about:
      'Sets HF_HUB_OFFLINE before the model library is imported. Not hardening: measured, a warm cache with no network and the variable unset fails with a message naming neither the network nor the cache. Set it false for the one run that warms the cache.',
    secret: false,
    measured: true,
  },

  /* ------------------------------------------------------------------ llm */
  {
    key: 'USHER_LLM_ENABLED',
    group: 'llm',
    def: 'false',
    about:
      'Off by default twice over: nine of ten row providers need no model, and this is the only setting whose on state sends the household’s data to a machine it may not own.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_LLM_BASE_URL',
    group: 'llm',
    def: 'http://localhost:8000/v1',
    about:
      'Any OpenAI-compatible endpoint. The default is localhost because a default naming a hosted provider would send a watch history off the box.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_LLM_API_KEY',
    group: 'llm',
    def: 'unset',
    about: 'Unset is a first-class value: a local vLLM or Ollama needs no credential.',
    secret: true,
    measured: false,
  },
  {
    key: 'USHER_LLM_MODEL',
    group: 'llm',
    def: 'gpt-4o-mini',
    about: 'Recorded on every llm_calls row, including the ones where no response came back.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_LLM_MAX_OUTPUT_TOKENS',
    group: 'llm',
    def: '2048',
    about:
      'A correctness setting rather than a cost one: a completion that hits the ceiling comes back as valid JSON with rows missing off the end, so the adapter refuses finish_reason "length". It fired live against a degenerate loop that ran the whole budget.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_LLM_TIMEOUT_SECONDS',
    group: 'llm',
    def: '120.0',
    about: 'Roughly 20× the slowest measured completion, 6.5 s. A generation is a background job.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_LLM_PRICE_IN_PER_MTOK',
    group: 'llm',
    def: '0',
    about:
      'Dollars per million input tokens, because no provider reports cost — usage carries token counts and nothing else. Zero is the honest value for a local model and the wrong one for a hosted model nobody priced.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_LLM_PRICE_OUT_PER_MTOK',
    group: 'llm',
    def: '0',
    about: 'Dollars per million output tokens. Spend is recomputable from the ledger afterwards.',
    secret: false,
    measured: false,
  },

  /* ------------------------------------------------------------- curation */
  {
    key: 'USHER_CURATION_POOL_SIZE',
    group: 'curation',
    def: '200',
    about:
      'Candidates in one generation’s prompt. Measured at 20.40 tokens per candidate; the reference 16k endpoint serves 600 and refuses 700 with an HTTP 400, because the real bound is prompt plus max output.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_QUERY_EXPANSION_ENABLED',
    group: 'curation',
    def: 'false',
    about:
      'Off because it measured worse: MRR 0.733 → 0.373 and recall@10 0.800 → 0.533 over five mood queries and 150 real overviews, with the five queries coming back more alike than they went in.',
    secret: false,
    measured: true,
  },

  /* --------------------------------------------------------------- search */
  {
    key: 'USHER_SEARCH_RESULT_LIMIT',
    group: 'search',
    def: '50',
    about: 'The ceiling on a request’s limit, not its default. Every candidate is assembled in code.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SEARCH_RRF_K',
    group: 'search',
    def: '60',
    about:
      'Reciprocal rank fusion’s smoothing constant, 1/(k+rank). 60 is the value RRF’s original paper uses. Not the constant the relevance term uses, which is 1.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SEARCH_HNSW_EF_SEARCH',
    group: 'search',
    def: '200',
    about:
      'Recall@10 over 132,409 real vectors and 12 typed queries: 40 → 0.700, 100 → 0.858, 200 → 0.917, 400 → 0.967. 400 is refused on cost, not on recall — its p50 of 20 ms would make the vector half four times the embed.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_SEARCH_TRIGRAM_THRESHOLD',
    group: 'search',
    def: '0.3',
    about:
      'The similarity floor for the suggest path. 0 admits every row, which is the latency cliff the narrow path exists to avoid; 1.0 is LIKE.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SEARCH_SUGGEST_CANDIDATES',
    group: 'search',
    def: '200',
    about:
      'Trigram candidates collected before the levenshtein re-rank. Measured: 1,774 candidates against 300,000 rows is a 169× reduction in edit-distance calls.',
    secret: false,
    measured: true,
  },

  /* ---------------------------------------------------------------- lanes */
  {
    key: 'USHER_PUSH_ENABLED',
    group: 'lanes',
    def: 'true',
    about: 'The push lane switch, so lanes can move to a separate container by editing compose.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_WORKER_ENABLED',
    group: 'lanes',
    def: 'true',
    about:
      'The worker lane switch. Delivered through environment: rather than env_file: it was silently ignored for a milestone, and two workers steal each other’s live claims.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_STALE_AFTER_SECONDS',
    group: 'lanes',
    def: '90.0',
    about:
      'How long a channel may deliver nothing at all before it is torn down. Not a socket timeout: it detects a live peer that has stopped delivering, which is the failure measured when a handshake to a nonexistent path was upgraded and held open.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_PUSH_POLL_SECONDS',
    group: 'lanes',
    def: '5.0',
    about: 'How long one receive waits before reporting nothing yet. The staleness watchdog’s tick.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_BACKOFF_SECONDS',
    group: 'lanes',
    def: '5.0',
    about: 'The base of the reconnect backoff, before equal jitter.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_MAX_BACKOFF_SECONDS',
    group: 'lanes',
    def: '300.0',
    about: 'The ceiling that backoff climbs to.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_MAX_CONSECUTIVE_FAILURES',
    group: 'lanes',
    def: '5',
    about: 'After this many, supports_push is set false and the nightly walk carries the source.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_MAX_ITEMS_PER_EVENT',
    group: 'lanes',
    def: '50',
    about:
      'Above this, one event asks for a delta walk instead of resolving items one at a time. Emby emits a library-changed event during a scan naming thousands, and a request each against 1,126,789 items is a design defect rather than a slow path.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_PUSH_GAP_MIN_INTERVAL_SECONDS',
    group: 'lanes',
    def: '60.0',
    about:
      'The floor between two gap-closing delta walks. Zero means close the gap on every reconnect, which is expensive and correct.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_GAP_CLOSE',
    group: 'lanes',
    def: 'cursored',
    about:
      'What the gap-closer may do when the delta has no cursor. Cursored refuses a full walk and warns instead, which changes behaviour only for a deployment that has never completed one.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PUSH_GAP_MAX_ITEMS',
    group: 'lanes',
    def: '20000',
    about:
      'The ceiling on one gap-closing delta that does have a cursor, counted in items; zero is unlimited. 20,000 is 100 pages at the shipped page size and about ten minutes of upstream at the 6.04 s/page mean measured 2026-08-15, deliberately under the 28,934 items a 30-day delta returned on that library. A walk that stops here records FAILED, so no cursor advances and nothing it never reached is skipped — `usher sync --kind full` closes the rest. It bounds the item lane only.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_PUSH_SOURCE_REFRESH_SECONDS',
    group: 'lanes',
    def: '60.0',
    about:
      'How often the supervisor re-reads the source list, so a new source gets a lane without a restart.',
    secret: false,
    measured: false,
  },

  /* ------------------------------------------------------------------ sse */
  {
    key: 'USHER_SSE_HEARTBEAT_SECONDS',
    group: 'sse',
    def: '20.0',
    about:
      'A comment frame on an otherwise idle stream, so an idle stream is healthy. Bounded below 60 because nginx closes an idle connection at 60 s and Cloudflare at about 100.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_SSE_BUFFER_SIZE',
    group: 'sse',
    def: '256',
    about:
      'The replay ring for Last-Event-ID. A client offline longer than this is answered resync_required rather than replayed a partial stream.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_SSE_QUEUE_SIZE',
    group: 'sse',
    def: '64',
    about:
      'Per-subscriber queue depth. On overflow the queue is replaced with one resync_required, so this is a tolerance for a slow client rather than a delivery guarantee.',
    secret: false,
    measured: false,
  },

  /* --------------------------------------------------------------- images */
  {
    key: 'USHER_IMAGE_CACHE_DIR',
    group: 'images',
    def: 'data/images',
    about: 'Where proxied artwork is cached. The container’s is a bind mount, which compose owns.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_IMAGE_MAX_BYTES',
    group: 'images',
    def: '5242880',
    about:
      'Enforced while the response streams rather than against a Content-Length the sender controls. 5 MiB is above every byte this proxy can legitimately receive: the largest artwork measured anywhere was 4,731,805 bytes, and the largest rung a 563 KB median poster.',
    secret: false,
    measured: true,
  },
  {
    key: 'USHER_IMAGE_FETCH_TIMEOUT_SECONDS',
    group: 'images',
    def: '10.0',
    about:
      'One CDN fetch, with a person waiting at the other end. A cold image that has not arrived in ten seconds is better reported than waited for.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_IMAGE_CDN_BASE_URL',
    group: 'images',
    def: 'https://image.tmdb.org/t/p/',
    about:
      'The provider’s image host. A configured constant rather than a per-image configuration call, which would be a second round trip for a value that changes approximately never.',
    secret: false,
    measured: false,
  },

  /* ------------------------------------------------------------ telemetry */
  {
    key: 'OTEL_EXPORTER_OTLP_ENDPOINT',
    group: 'telemetry',
    def: 'unset',
    about:
      'The collector. Telemetry is never required: with none configured the exporters are no-ops and Usher runs normally. Read under OpenTelemetry’s own name, not USHER_.',
    secret: false,
    measured: false,
  },
  {
    key: 'OTEL_SERVICE_NAME',
    group: 'telemetry',
    def: 'usher',
    about:
      'The resource attribute every span and metric carries. Read under OpenTelemetry’s own name, not USHER_.',
    secret: false,
    measured: false,
  },

  /* -------------------------------------------------------------- console */
  {
    key: 'USHER_CONSOLE_ENABLED',
    group: 'console',
    def: 'true',
    about:
      'Whether this process also serves the console at /console. Off is a real deployment, not a debugging aid: a worker-only or push-only container has no browser pointed at it, and a household running its own client does not want a second one answering /.',
    secret: false,
    measured: false,
    observed: { value: 'true', proof: 'this page is being served' },
  },
  {
    key: 'USHER_CONSOLE_DIST_DIR',
    group: 'console',
    def: 'web/dist',
    about:
      'Where the built bundle lives, relative to the working directory. The same value in a checkout and in the container, because the Dockerfile copies vite build’s output to /app/web/dist rather than making an operator learn a second path. A missing bundle is not an error — the API is served alone and the miss is logged once.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_GRAFANA_URL',
    group: 'console',
    def: 'unset',
    about:
      'The deployment’s Grafana, for the Insights screen’s “Open in Grafana”. Unset renders as the link being absent rather than as a dead one — the same distinction this product makes everywhere between never computed and computed and empty. Never proxied through Usher; the browser follows it directly.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_TEMPO_URL',
    group: 'console',
    def: 'unset',
    about:
      'The deployment’s Tempo, for the “Open trace” link on a rendered problem document. Same rules as USHER_GRAFANA_URL. That one link is what PRD 10’s telemetry is for on a failure path: every response carries a trace id, and without somewhere to open it the id is a string an operator cannot act on.',
    secret: false,
    measured: false,
  },
]

/**
 * How many settings this console knows about.
 *
 * **Derived, never written down.** It read `69` in six places until 2026-08-19,
 * when four console settings were added to `Settings` and every one of those
 * six became wrong at once — a bare number that nothing recomputes is the
 * failure mode this product's own honesty rules exist to prevent, and it had
 * one about itself. `tests/unit/test_console_settings_catalogue.py` pins this
 * list against `Settings.model_fields`, so a setting added to the backend and
 * not to this file fails the backend suite rather than reaching a screen.
 */
export const SETTING_COUNT = CONFIG.length
