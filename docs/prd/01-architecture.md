# 01 — Architecture

## Shape: modular monolith, hexagonal core

One deployable application. Ports-and-adapters internally, so the pieces are
independently testable and swappable.

```
   [household clients]   [Alfred]   [Home Assistant]
              │              │            │
              └──────────────┴────────────┘
                             │  HTTP + SSE
┌────────────────────────────▼──────────────────────────────┐
│ Usher                                                     │
│                                                           │
│  api/         routers · dependencies · DTOs · lanes       │
│  services/    catalog · ingest · match · enrich ·         │
│               search · rows · curation · watchstate ·     │
│               bootstrap · jobs                            │
│  domain/      Pydantic models — the canonical language    │
│  ports/       SourceAdapter · MetadataProvider ·          │
│               SearchIndex · SuggestIndex · Embedder ·     │
│               LLMClient · TitleRepository · Row ·         │
│               RowProvider (all ABCs)                      │
│  adapters/    emby/ · tmdb/ · bulk/ ·                     │
│               search/ · embedding/ · llm/ · images/       │
│  db/          SQLAlchemy 2.0 async · Alembic · repos      │
└────────────────────────────┬──────────────────────────────┘
                             ▼
                       [PostgreSQL]
             canonical catalog · search · vectors · job queue
```

`adapters/` subdirectories are named for the upstream service when a port's
implementation talks to one nameable external service (`emby/` →
`SourceAdapter`, `tmdb/` → `MetadataProvider`) and for the capability otherwise
(`bulk/`, `search/`, `embedding/`, `llm/`, `images/`). **`adapters/postgres/`
does not exist and must not be created** — it would put a `SearchIndex` and a
repository in one directory.

**Deployment:** `compose.yml` with `usher` + `postgres`. One stateful service.
**There is no `meilisearch` service.** What exists is a feature gate behind the
`SuggestIndex` port, off by default.

## Layering rules

1. **`domain/` imports nothing from `adapters/`, `db/`, or `api/`.** Pure
   Pydantic models and value objects.
2. **`services/` depends only on `domain/` and `ports/`.** A service never
   imports an adapter; it receives one. Repositories are ports too.
3. **`adapters/` implement `ports/` and may import `domain/`.** Raw Emby or
   TMDb JSON never escapes its adapter package.
4. **`db/` models are separate from `domain/` models.** Repositories translate,
   and implement the repository ports declared in `ports/`.
5. **`api/` maps domain models to response DTOs.** Wire format is versioned
   independently of internal models.

Import discipline is enforced in CI by `import-linter`: **Twelve contracts**.
`usher.api.routers` may not name `usher.composition`,
`usher.services.curation` or `usher.ports.llm`; it reaches the wiring through
`usher.api.deps`.

## Ports are ABCs, not Protocols

All ports are `abc.ABC` with `@abstractmethod`. The shared httpx pieces live in
`src/usher/adapters/http.py`; the client lifecycle is per-adapter.

### Outbound calls and their limiters

**Nine modules** under `src/usher/adapters/` dial an upstream: **eight over
httpx**, between them **sixteen call sites**, and a ninth — `emby/push.py` —
over `websockets`. Three of the nine are paced; six deliberately are not.

| module → upstream | limiter |
|---|---|
| the configured media source (`emby/session.py`) | the per-source minimum-interval gate, `USHER_SOURCE_REQUESTS_PER_SECOND` |
| `api.themoviedb.org` (`tmdb/client.py`) | `_TokenBucket` at `USHER_TMDB_REQUESTS_PER_SECOND` |
| `api.themoviedb.org` (`tmdb/provider.py`, six call sites) | the bucket above — this module holds no client of its own |
| `/embywebsocket` (`emby/push.py`) | **none** — a socket held open is not a request; the reconnect *backoff* is its limiter |
| `image.tmdb.org` (`images/provider.py`) | **none** — the CDN publishes no limit and the cache is the bound |
| the IMDb/TMDb/MovieLens dataset hosts (`bulk/download.py`, two call sites) | **none** — one streamed file per dataset plus one `HEAD` for its revision |
| `query.wikidata.org` (`bulk/wikidata.py`) | **none** — a bootstrap phase run by hand, chunked and sequential |
| `USHER_LLM_BASE_URL` (`llm/openai_compatible.py`) | **none** — `curate` is capped at 1 in flight |
| `USHER_EMBEDDING_MODEL`'s endpoint (`embedding/openai_compat.py`) | **none** — `index` is capped at 1 in flight |

### Ports and their implementations

| Port | Implementations (v1) |
|---|---|
| `SourceAdapter` | `EmbyAdapter` |
| `MetadataProvider` | `TmdbMetadataProvider` |
| `BulkDataset` | `IMDbDumps`, `TMDbIdExport`, `WikidataCrosswalk`, `MovieLensGenomeDataset` |
| `SearchIndex` | `PostgresSearchIndex` (`MeilisearchIndex` gated) |
| `SuggestIndex` | `PostgresSuggestIndex` (`MeilisearchSuggestIndex` gated) |
| `Embedder` | `FastEmbedEmbedder`, `OpenAICompatEmbedder` — **optional**, off by default; a deployment without one still has full-text and trigram |
| `LLMClient` | `OpenAICompatibleClient` — one `POST /v1/chat/completions`; the provider abstraction is `USHER_LLM_BASE_URL` |
| `TitleRepository` | `PostgresTitleRepository` |
| `Row` | `BaseRow` in `services/rows/base.py` and its **ten** concrete rows |
| `RowProvider` | `ContinueWatchingProvider`, `NextUpProvider`, `RecentlyAddedProvider`, `RediscoverProvider`, `BecauseYouWatchedProvider`, `FranchiseProvider`, `GenreAffinityProvider`, `SeasonalProvider`, `PeopleProvider`, `CuratedProvider` — **ten**, registered as `services/rows/__init__.py`'s `ROW_PROVIDERS` |

## Repository layout

```
usher/
├── docs/
│   ├── prd/                    ← this
│   ├── plans/                  ← task breakdowns
│   └── specs/                  ← reviewed design specs
├── src/usher/
│   ├── api/         routers/, deps.py, dto/, lanes.py
│   ├── domain/      title.py, person.py, source.py, watch.py, rows.py,
│   │                curation.py
│   ├── ports/       *.py  (ABCs only), repository/
│   ├── adapters/    emby/, tmdb/, bulk/, search/, embedding/, llm/, images/
│   ├── services/    rows/ (base.py, cache.py, one module per provider),
│   │                home.py, taste.py, derive.py, similar.py, search.py,
│   │                matching.py, ingest.py, enrich.py, push.py, jobs.py,
│   │                scheduler.py, curation*.py, backup.py, restore.py
│   ├── eval/        the quality-eval harness (optional extra)
│   ├── db/          models/, repositories/ (implement ports/), migrations/
│   ├── composition.py, config.py, telemetry.py, cli.py
├── web/             the Usher Console, served at /console
├── tests/           unit/, integration/, fixtures/, fakes/
├── compose.yml
└── pyproject.toml
```

There is no `jobs/` package: the priority queue is `ports/jobs.py` +
`db/repositories/jobs.py`, the worker is `services/jobs.py`, and scheduling is
`services/scheduler.py` plus `api/lanes.py`'s supervised lanes.

Files stay small and single-purpose.

## Stack

| | |
|---|---|
| Language | Python 3.13 |
| Web | FastAPI + uvicorn |
| Models | Pydantic v2 |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| DB | PostgreSQL 17 + pgvector ≥ 0.8.5 |
| Jobs | In-process asyncio workers over a Postgres-backed queue |
| LLM | Any OpenAI-compatible endpoint, over httpx — `USHER_LLM_BASE_URL` |
| Embeddings | fastembed (local, optional, 167 MiB, no torch) or any OpenAI-compatible endpoint |
| Console | React 19 + Vite, in `web/` |
| Packaging | uv |
| License | MIT |

## Concurrency model

Everything is asyncio in one process. Work is separated by lane, each with its
own bound, so a slow upstream cannot starve the API.

| Lane | Concurrency | Bounded by |
|---|---|---|
| API request handling | uvicorn default | — |
| Source event stream | 1 per source | push connection |
| **Job worker, globally** | **`USHER_JOB_CONCURRENCY`, default 12** | the connection pool; `Settings` refuses a value it cannot serve |
| — `enrich` | the global (12) | TMDb, at `USHER_TMDB_REQUESTS_PER_SECOND` |
| — `match`, `watch_history`, `watch_writeback` | 4 | measured throughput knee |
| — `derive` | 4 | measured throughput knee |
| — `index` | 1 | embedding is CPU-bound |
| — `curate` | 1 | one completion at a time |
| — `sync`, `bootstrap` | 1 | a walk of the whole library |
| Embedding | the `index` row above; `USHER_EMBEDDING_BATCH_SIZE` is its batch | CPU/GPU |
| **Row build** | **1, sequential — not a setting** | `AsyncSession` |
| **Screen refresh** | **1 lane, 1 refresh in flight, ≤ 32 keys queued** | `REFRESH_QUEUE_SIZE`; full means dropped |

A concurrency entry is a slot count, not a request rate: the per-source gate
`USHER_SOURCE_REQUESTS_PER_SECOND` (default 0.4) bounds the wire. Every job in
flight holds an `AsyncSession`, so `Settings` refuses a `job_concurrency` the
`USHER_DB_POOL_SIZE` pool cannot serve. `curate` is capped at one completion at
a time, up to `USHER_LLM_TIMEOUT_SECONDS` (120 s).

Screen refresh drops rather than blocks when its queue is full, costing one
cache miss. `index` jobs run at `BACKFILL` priority, or at the rung their
`enrich` job was claimed at when that is `VISIBLE` or above.

A `--worker` entrypoint flag moves the lanes to a separate container.

## Extension seams left open in v1

Deliberately designed-for but not built:

- **Authentication.** All routes take a `current_user` dependency that returns
  the singleton default user in v1. Adding real auth replaces one dependency;
  watch state and taste are already per-user.
- **Additional sources.** `MediaItem` is many-per-title from the start.
- **Additional metadata providers.** Provider precedence is a config list; field
  provenance is recorded per title.
- **Alternative search backends.** `SearchIndex` and `SuggestIndex` are ports.
