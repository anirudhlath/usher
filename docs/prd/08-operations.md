# 08 — Operations

## Configuration

Three layers, split by what changes and when:

| Layer | Holds | Changes |
|---|---|---|
| **Environment** | `DATABASE_URL`, port, log level, embedding model, `USHER_SECRET_KEY`, TMDb key | Deploy time |
| **Config file** (TOML) | Rate limits, TTLs, row weights, enrichment tier, concurrency per lane, image cache ladder | Restart |
| **Database** | Sources, users, row provider enable/disable | Runtime, via admin API |

Sources live in the database because they are added through the admin API. A
deployment that needs a compose edit and a restart to connect a media server is
the wrong shape for this.

### Secrets

Source credentials are **encrypted at rest in Postgres**, using a key supplied
via `USHER_SECRET_KEY` (environment or Docker secret). `Source.credentials_ref`
points at the encrypted row; the plaintext exists only in memory in the adapter
that needs it.

Rules:

- Credentials are never returned by any API, including admin. Write-only.
- Credentials are never logged, including in error paths and request dumps.
- Rotating `USHER_SECRET_KEY` re-encrypts on next write; a documented rotation
  command handles the bulk case.
- No credential ever reaches a client. This is the failure of the setup Usher
  replaces, where a raw Emby token lived in browser-delivered dashboard config.

## Failure and degradation

**A degraded subsystem narrows functionality; it never fails a request that
local state can answer.**

| Failure | Behaviour |
|---|---|
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong. |
| Push socket drops | Backoff reconnect; delta reconcile on reconnect; after N failures mark `supports_push = false` and lean on the nightly walk. |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected. |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades. |
| LLM call fails | Previous curated rows persist. Home composes without them. |
| Embedder unavailable | Semantic search falls back to full-text, flagged in the response. |
| Meilisearch down (if enabled) | Fall back to the Postgres index. It is never the only index. |
| Postgres down | Total outage. The one hard dependency, deliberately. |

## Job reliability

Postgres-backed queue, claimed with `SELECT … FOR UPDATE SKIP LOCKED`.

- Exponential backoff with jitter; per-job attempt counter.
- **Poison threshold** — after N attempts a job is *parked* with its error, not
  retried forever and not silently dropped.
- Parked jobs are listed in the admin API and counted in metrics. Silent failure
  is the thing worth engineering against; visible failure is fine.
- Jobs are idempotent by construction, so redelivery is always safe.
- Startup requeues anything left `in_progress` by an unclean shutdown.

## Observability

Structured JSON logging via loguru, matching Alfred.

The metrics that actually predict problems:

| Metric | Why |
|---|---|
| Queue depth by priority | Enrichment falling behind demand |
| Enrichment latency p50/p99 | The read-through promise in [03](03-sources-and-sync.md) |
| Sync run outcome and duration | Source health over time |
| Push connection uptime and reconnects | Whether push is actually working |
| Parked job count | Systematic failure hiding behind retries |
| Search latency p50/p99 by mode | The [05](05-search-and-similarity.md) upgrade gate |

`GET /health` is liveness; `GET /health/ready` reports Postgres, migration
state, and per-source connectivity — degraded rather than binary, so a
dashboard can distinguish "down" from "running without Emby".

## Testing

| Layer | Approach |
|---|---|
| **Unit** | Services against port fakes. No network, ever. Fakes are trivial because ports are ABCs. |
| **Integration** | Real Postgres (testcontainers). Recorded provider payloads committed as fixtures — never live API calls in CI. |
| **Adapter contract suite** | One parametrised test class every `SourceAdapter` must pass. |
| **Bootstrap** | Small committed slices of each dataset. Never a full download in tests. |
| **API** | Schema-validated request/response round-trips against the OpenAPI contract. |

**The contract suite is the load-bearing one.** It is what proves the
abstraction is real rather than aspirational: when a Jellyfin adapter is
written, it either passes the same tests the Emby adapter passes, or the port
was wrong. Everything else is ordinary testing.

Development follows TDD — failing test first, then implementation.

## Deployment

```yaml
services:
  usher:
    build: .
    environment: [DATABASE_URL, USHER_SECRET_KEY, TMDB_API_KEY]
    volumes: ["./data/images:/data/images", "./data/models:/data/models"]
    depends_on: { postgres: { condition: service_healthy } }
  postgres:
    image: pgvector/pgvector:pg17
    volumes: ["./data/postgres:/var/lib/postgresql/data"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U usher"] }
```

- Alembic migrations run on startup; the app refuses to serve on a schema
  mismatch rather than guessing.
- First run detects an empty catalog and offers bootstrap through the admin API
  — it does not start a multi-hour download unprompted.
- Bootstrap is resumable and checkpointed; a restart mid-import continues.

### Backup — the asymmetry is the point

| Rebuildable from importers | Precious |
|---|---|
| Catalog, embeddings, search index, neighbour tables, cached images, curated rows | **Watch state**, users, source config, manual unmatched resolutions |

The precious set is a handful of small tables. A documented `pg_dump` of those
turns disaster recovery into a short restore plus a background rebuild, instead
of a crisis. State this loudly in the README — it is the difference between
"lost everything" and "lost an afternoon of indexing".

### Resource envelope

| | |
|---|---|
| Postgres | ~8–12 GB catalog + indexes; ~1.5 GB HNSW (`halfvec`) |
| Image cache | Grows with use; capped by a configurable LRU ceiling |
| Usher process | ~500 MB–1 GB, plus ~200 MB for the embedding model |
| Embedding model | ~130 MB on disk |

Tuning that matters: `maintenance_work_mem` high enough to avoid the
`hnsw graph no longer fits into maintenance_work_mem` notice during index
builds, `max_parallel_maintenance_workers = 7`, and GIN `fastupdate = off`.
