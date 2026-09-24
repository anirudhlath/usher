# Configuration

Usher reads its settings from `.env`. [`.env.example`](../../.env.example)
lists every setting with its default and what it does, so copy it and edit it:

```sh
cp .env.example .env
```

Every `USHER_*` key is validated when Usher starts. An unknown key, such as a
typo, is refused rather than ignored. A rejected setting is reported by name
but not by value, because it may be a credential.

## The two settings you must fill in

### `USHER_SECRET_KEY`

Encrypts stored source credentials and playback tickets. It has no default,
and compose refuses to start without it. Generate one with
`openssl rand -hex 32`.

Don't edit it in place once sources are registered, because their stored
credentials become unreadable. Follow the
[rotation runbook](../runbooks/rotation.md) instead.

### `USHER_TMDB_API_KEY`

Enrichment: overviews, artwork, cast and crew. Without it, Usher logs
`no TMDb API key configured; enrich and derive jobs will not be claimed` once at
startup and enriches nothing.

## Editing `.env`

**Change a line where it is; don't add a second copy.** Every key in
`.env.example` already has a line. When a key appears twice, the later line
wins, so a copy pasted near the top loses to the original further down, and
nothing tells you.

**Compose reads `.env` when it creates the container.** After a change, run
`docker compose up -d` so the container is recreated with the new values.

**Every key in `.env` reaches the container**, because compose hands it the
whole file (`env_file:`). The five exceptions are marked `[compose-owned]` in
`.env.example`, and `compose.yml` sets them itself, so editing them in `.env`
changes nothing: `USHER_DATABASE_URL`, `USHER_HOST`, `USHER_PORT`,
`USHER_IMAGE_CACHE_DIR` and `USHER_BULK_DATA_DIR`.

| Compose-owned key | Set to |
|---|---|
| `USHER_DATABASE_URL` | The `postgres` service, by its hostname on the compose network. |
| `USHER_HOST`, `USHER_PORT` | `0.0.0.0` and `8000`, which the published port and the healthcheck assume. |
| `USHER_IMAGE_CACHE_DIR`, `USHER_BULK_DATA_DIR` | `/data/images` and `/data/bulk`, the container side of two bind mounts. |

`USHER_SECRET_KEY` also appears in `compose.yml`, but it carries your own value.
It is repeated there so that a missing key fails `docker compose up` instead of
failing in the container's log.

Keys that start with `USHER_COMPOSE_`, and compose's own `COMPOSE_*` variables,
are read by compose rather than by Usher:

| Setting | Default | What it does |
|---|---|---|
| `USHER_COMPOSE_HOST_PORT` | `8100` | The host port the API and console are published on. Use `IP:port`, such as `127.0.0.1:8100`, to publish on one address only. |
| `USHER_COMPOSE_NETWORK` | `usher_default` | The name of the stack's network. |
| `COMPOSE_PROJECT_NAME` | the directory's name | The compose project, and the prefix of every container name. |
| `COMPOSE_FILE` | unset | Which compose files to apply. Used to opt into [telemetry](#telemetry). |

## Data directories

Compose bind-mounts three host directories:

| Directory | Holds | Notes |
|---|---|---|
| `data/postgres` | The database | Survives `docker compose down -v`. |
| `data/bulk` | Downloaded datasets, and any backup you write there | The datasets are safe to delete, because the next bootstrap downloads them again. Move backups out first. |
| `data/images` | Artwork the image proxy has fetched | Up to four sizes per image. Nothing evicts entries, so it grows with what you browse. Reclaim space with `rm -rf data/images`, which only costs a re-fetch. |

The container runs as uid 1000, so `data/bulk` and `data/images` must belong to
it before the first `up`:

```sh
mkdir -p data/images data/bulk && sudo chown 1000:1000 data/images data/bulk
```

## A second stack on one host

A second checkout started with the defaults takes over the first one's
containers or network. Before the second stack's first `up`, set these three
keys in its `.env`, with names of your own:

```dotenv
COMPOSE_PROJECT_NAME=usher-scratch
USHER_COMPOSE_NETWORK=usher-scratch_default
USHER_COMPOSE_HOST_PORT=8101
```

`.env` already has the last two lines, so change them where they are, and add
the first.

- **The project name** decides which containers an `up` touches. A clone left
  in a directory called `usher` is project `usher`, so its `up` recreates the
  first stack's containers with the second stack's config.
- **The network name** defaults to `usher_default` rather than following the
  project name, so a second stack that keeps it joins the first one's network.
  Both `postgres` containers then answer to the same hostname, and either stack
  can silently read and write the other's database.
- **The port** is the one the second API is published on.

## Semantic search

Full-text search and type-ahead cover the whole catalog without a model.
Embeddings add semantic search, "more like this", and a similarity signal for
the home screen. Only titles enriched from TMDb are embedded.

The container image carries no optional dependencies, so in Docker the model
runs behind an OpenAI-compatible embeddings endpoint, such as vLLM or Ollama:

```dotenv
USHER_EMBEDDING_ENABLED=true
USHER_EMBEDDING_MODEL=openai:BAAI/bge-m3
USHER_EMBEDDING_BASE_URL=http://embeddings.lan:8001/v1
USHER_EMBEDDING_API_KEY=
```

- **The model must return 1024-dimensional, unit-length vectors.** An `openai:`
  model is checked only on the first batch after startup, and the wrong model
  parks that index job. After the first backfill, look for parked jobs in
  `usher sync-status`.
- **The base URL is resolved inside the container**, so `localhost` means the
  container itself.
- **Changing the model marks every stored vector stale.**

Then embed the catalog, and build the neighbour table from the vectors:

```sh
docker compose exec usher usher index --backfill
docker compose exec usher usher similar --rebuild
```

The first command queues the work, and the server's worker lane drains it.
`usher index` with no flag reports how much is still stale. Run `similar
--rebuild` once the index has drained.

A source install can run the model in-process instead. `uv sync --extra
embedding` installs fastembed (167 MiB, no torch), and the default
`USHER_EMBEDDING_MODEL` is already a 1024-dimensional fastembed model. Set
`USHER_EMBEDDING_OFFLINE=false` for the first run so the model downloads
(about 1.2 GB), then set it back to `true`.

## LLM-curated rows

Curated rows are home-screen rows that a language model picks from a pool of
candidates Usher builds. Usher asks an OpenAI-compatible chat endpoint for
structured output (`response_format` of type `json_schema`, with
`strict: true`), so the server must support that. Only vLLM has been tested.

```dotenv
USHER_LLM_ENABLED=true
USHER_LLM_BASE_URL=http://llm.lan:8000/v1
USHER_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
USHER_LLM_API_KEY=
USHER_LLM_PRICE_IN_PER_MTOK=0
USHER_LLM_PRICE_OUT_PER_MTOK=0
```

- **Always set the base URL.** It is resolved inside the container, and the
  default, `http://localhost:8000/v1`, is Usher's own port there.
- **Set the two prices** in dollars per million tokens. No endpoint reports
  cost, so Usher computes it from these and records it on every row of
  `llm_calls`. Left at `0`, your spend reads as zero. Token counts are recorded
  exactly, so spend can be recomputed later.
- **The candidate pool and the output ceiling share one context window.** Raising
  `USHER_CURATION_POOL_SIZE` (default 200) or `USHER_LLM_MAX_OUTPUT_TOKENS`
  (default 2048) past what your model's context holds fails the call with an
  HTTP 400, and the job is parked.
- **Nothing schedules a generation.** Run `usher curate` from cron. It exits 1
  when a night produces no rows. See the
  [command-line guide](command-line.md#llm-curation).

`USHER_QUERY_EXPANSION_ENABLED=true` makes semantic search rewrite a query with
the LLM before embedding it. It needs `USHER_LLM_ENABLED=true`, and Usher
refuses to start with one on and not the other. It is off by default, because
rewriting made retrieval worse on the one model it was measured against.

## The scheduler

`USHER_SCHEDULER_ENABLED=true` runs two periodic jobs inside the server:

- pruning `search_queries` past `USHER_SEARCH_QUERY_RETENTION_DAYS` (90);
- `usher similar --rebuild`, every `USHER_SIMILAR_REBUILD_PERIOD_HOURS` (24).

It is off by default. Turn it on in exactly one process, or run
`usher schedule --once` from cron instead.

## Telemetry

Usher exports traces and metrics over OTLP/gRPC when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, and builds no exporter when it is empty,
which is the default.

To reach a collector running in another compose stack, join that stack's
`observability` network. Set two keys in `.env`:

```dotenv
COMPOSE_FILE=compose.yml:compose.observability.yml
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

- **`COMPOSE_FILE` is a new line.** `OTEL_EXPORTER_OTLP_ENDPOINT` already exists
  and is blank, so fill in that line.
- **The network must already exist.** Run `docker network create observability`,
  or start the telemetry stack first.
- **Put `COMPOSE_FILE` in `.env` rather than passing `-f` flags.** An `up` that
  forgets the flags silently recreates the container without the network.
- **List your `compose.override.yml` too, if you have one.** Once
  `COMPOSE_FILE` is set, compose stops loading it by itself:
  `COMPOSE_FILE=compose.yml:compose.override.yml:compose.observability.yml`.

The dashboards and alert rules are in [`dashboards/`](../../dashboards/README.md).
[PRD 10](../prd/10-telemetry-and-dashboards.md) describes what is instrumented.

## The console

The web console is served by the same process at `/console`, and `/` redirects
to it.

| Setting | Default | What it does |
|---|---|---|
| `USHER_CONSOLE_ENABLED` | `true` | Set `false` for a worker-only container, or if you run your own client. |
| `USHER_GRAFANA_URL` | empty | Shows "Open in Grafana" on the Insights screen. |
| `USHER_TEMPO_URL` | empty | Shows "Open trace" on a failed request. |

The browser follows the two links directly; Usher does not proxy them.
