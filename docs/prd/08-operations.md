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
  Structurally, not by discipline: `POST /admin/sources` parses a username
  and password into a request DTO, and **no response DTO in `api/dto/` has
  a field either could be assigned to** — there is nothing to forget to
  omit. Enforced over the whole package, not per model, so a response type
  added by a later milestone inherits the rule.
- Credentials are never logged, including in error paths and request dumps.
- **A rejected request never echoes the body it rejected.** This one is not
  free, and it is not covered by `SecretStr`: FastAPI's default `422`
  answers with pydantic's errors, and a `missing` error carries the whole
  *unparsed* request dict in its `input` field — every sibling value, as
  submitted, before any of them became a `SecretStr`. Omitting a single
  field from an otherwise valid `POST /admin/sources` therefore made the
  server reply with the plaintext password. `usher.api.errors` strips
  `input` from every validation error, app-wide.
- Rotating `USHER_SECRET_KEY` re-encrypts on next write; a documented rotation
  command handles the bulk case. **Until that write happens the old rows are
  unreadable, and that state is rendered rather than raised**: Fernet's
  authentication tag makes a wrong key a diagnosable `PortDataMalformed`, and
  `GET /admin/sources/{id}/status` reports it as an unreachable,
  unauthenticated source with a re-enter-your-credentials detail — the
  screen an operator would open to work out what broke must not answer with
  a `500`. The rendered detail is a fixed string, never the exception's own,
  because that one names the `credentials_ref` so an operator can find the
  row: right for a log line, wrong for a response body.
- No credential ever reaches a client. This is the failure of the setup Usher
  replaces, where a raw Emby token lived in browser-delivered dashboard config.
  **One documented exception in v1: a `direct` playback target's URL carries
  the source's session token**, because Usher never proxies the bytes and the
  route that serves them authenticates — verified: strip the token from that
  URL and Emby answers 401. It no longer carries Usher's own `DeviceId`
  alongside it; the same route answers 206 without one, so that parameter is
  simply not sent (2026-07-31). See
  [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md) for what
  that grants, why the two halves of the original failure are not equally
  present, the risks accepted with it, and the M9 playback ticket that narrows
  it — a `302` moves the token out of the response body and into a `Location`
  header, which makes the shareable artifact opaque and short-lived rather
  than removing the grant.
- **The exception reaches the first rule above, too.** `POST /titles/{id}/play`
  returns that token, so "never returned by any API" holds for the stored
  username and password and for every other credential Usher holds, and not
  for this one. What still binds it without exception: never logged (enforced
  once, on the DTO that carries it, rather than by each caller), never a span
  attribute, and never written to a table, a cache, or a file. It is **not**
  minted per request — the session token is cached in memory for the adapter's
  lifetime and re-minted only on a 401 ([03](03-sources-and-sync.md)), so
  there is no rotation and the grant outlives the response that carried it.
- At the config layer, `database_url`, `secret_key`, and `tmdb_api_key` are
  held as `pydantic.SecretStr` and unwrapped only at the point of use, so the
  rules above are enforced by the type system, not just convention.
- **"Never logged" has to cover libraries Usher hands a credential to, not
  just Usher's own log lines.** From M5 the source token is also the query
  string of a `websockets` URL, and that client debug-logs its own request
  line — so at `USHER_LOG_LEVEL=DEBUG` the rule was broken by code this
  project does not own. Measured against the real library before it was
  fixed. The guard is a logger whose *level* is above `CRITICAL`, because
  `configure_logging` clears `handlers` and re-forces `propagate = True` on
  every logger and never touches `level`; it is re-asserted on every
  connect, and it costs the library's own handshake and frame diagnostics.
  [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md) records
  the reproduction and the two other lines through that logger that could
  carry the same URL.

## Failure and degradation

**A degraded subsystem narrows functionality; it never fails a request that
local state can answer.**

| Failure | Behaviour |
|---|---|
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong. |
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. Catalog unaffected. |
| Push socket drops | Backoff reconnect; delta reconcile on reconnect; after N failures mark `supports_push = false` and lean on the nightly walk. |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected. |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades. |
| LLM call fails | Previous curated rows persist. Home composes without them. |
| Embedder unavailable | Semantic search falls back to full-text, flagged in the response. |
| Meilisearch down (if enabled) | Fall back to the Postgres index. It is never the only index. |
| Postgres down | Total outage. The one hard dependency, deliberately. |

## Job reliability

Postgres-backed queue, claimed with `SELECT … FOR UPDATE SKIP LOCKED`.

- Exponential backoff with jitter; per-job attempt counter. The jitter is
  **equal jitter** — the delay is a uniform draw from
  `[base/2, base) × 2^attempts` — not the more commonly cited *full* jitter,
  which draws from `[0, base) × 2^attempts`. Full jitter's minimum draw is
  arbitrarily close to zero, so a share of failures against a broken upstream
  retry effectively immediately: the hot loop the backoff exists to prevent,
  merely rationed. The spread is what breaks a thundering herd, and a
  half-interval floor keeps all of it while making "a failed job is not
  instantly re-claimable" a property rather than a probability. Implemented in
  `usher.db.repositories.jobs`, one `CASE` inside the failure statement;
  `job_backoff_seconds` is the base.
- **Malformed data does not back off at all — it parks on the first attempt.**
  `PortDataMalformed` means the upstream answered and the answer was wrong, so
  five identical retries only delay a human seeing it by the whole backoff
  schedule. `JobQueue.fail(retryable=False)` is that path, and it is distinct
  from the poison threshold below: this one reports `attempts == 1`.
- **Poison threshold** — after N attempts a job is *parked* with its error, not
  retried forever and not silently dropped. `job_max_attempts` is N, and
  "after N attempts" means exactly N.
- **Work that has become impossible *completes*, and does not park.** A job
  naming an item its source has since deleted, or one no configured source
  addresses, is not poison — parking it fills the review list with things that
  are simply gone, and a parked job needs a human to release it. Parking is
  reserved for work a human has to look at. The three handlers
  (`usher.services.handlers`) are where this is decided; a job whose *key* is
  unparseable is the opposite case and does park, because that is a real
  defect somebody has to see.
- **Re-enqueueing does not un-park.** Poison a human has not looked at is not
  fixed by asking for it again, and a parked job's priority is not promoted
  behind their back either.
- **Re-enqueueing work that has not changed writes nothing.** A nightly walk
  enqueues a job for every item it saw, so an `ON CONFLICT DO UPDATE` with no
  `WHERE` rewrites a row per job per night — 1,126,674 dead-weight row
  versions at the one measured deployment, plus the WAL and the vacuum, on a
  table whose entire purpose is to stay small — while changing nothing
  anybody can observe. The update fires only on a genuine promotion
  (`jobs.priority < excluded.priority`), and `enqueue` reports 0 rows written
  otherwise, which is the honest number.
- **A backed-off job is still `pending`, and nothing bounds the claim scan
  over them.** `ix_jobs_claim` is `(priority DESC, created_at) WHERE status =
  'pending'`, and `run_after <= clock_timestamp()` cannot be an indexed
  predicate (`clock_timestamp()` is not immutable) — so a queue whose jobs
  have all backed off against a broken upstream makes every claim walk past
  all of them. Measured against `pgvector/pgvector:pg17`: 1,126,674
  backed-off jobs plus one runnable one is a 216 ms claim with `Rows Removed
  by Filter: 1126674`. Recorded rather than solved: putting `run_after`
  first destroys the priority ordering the queue exists for, and the
  condition only arises when an upstream is broken — at which point a slow
  claim is not the problem.
- Parked jobs are listed in the admin API and counted in metrics. Silent failure
  is the thing worth engineering against; visible failure is fine.
- Jobs are idempotent by construction, so redelivery is always safe.
- Startup requeues anything left `in_progress` by an unclean shutdown.

## Observability

loguru for logs, OpenTelemetry for metrics and traces, and Grafana over three
datasources. Instrumentation, metric catalogue, dashboards, and alerts are
specified in [10](10-telemetry-and-dashboards.md).

Telemetry is optional: with no OTLP endpoint configured the exporters are
no-ops and Usher runs normally.

`GET /health` is liveness; `GET /health/ready` reports Postgres and
migration state — and **gates its status code on those two alone**. Lane
state is reported in the body (`lanes.push`, `lanes.worker`) and per-source
push health at `GET /admin/sources/{id}/status`'s `push_available`, never in
the code. That is a correction to what this section said before M5 built the
lanes: a readiness probe that failed because a source was unreachable would
take the process out of a load balancer for a reason restarting it cannot
fix, which is the same argument that keeps liveness off the database. The
failure table above already prices an unreachable source as "catalog fully
browsable"; a 503 would contradict it.

The report is still degraded rather than binary, so a dashboard can
distinguish "down" from "running without Emby" — it just does so by reading
the body, which is what a dashboard does and what Kubernetes, Docker
`healthcheck` and a load balancer never do.

Lane state is free to report — `lanes.push` is the set of running lane
*tasks*, and `push_available` is an in-memory ledger of messages received,
not a probe — so readiness makes **no upstream request at all**. The shipped
compose healthcheck polls this endpoint every 2 s against a source
[01](01-architecture.md) measures at 1–5 s per request. The on-demand probe
that *does* open a socket is `usher push --probe`, and it reports what
arrived rather than that the handshake succeeded
([ADR-0004](decisions/0004-push-over-polling.md)).

## Testing

| Layer | Approach |
|---|---|
| **Unit** | Services against port fakes. No network, ever. Fakes are trivial because ports are ABCs. |
| **Integration** | Real Postgres (testcontainers). Provider payloads committed as fixtures — *shape*-recorded and value-synthetic, never a capture; never live API calls in CI. |
| **Adapter contract suite** | One parametrised test class every `SourceAdapter` must pass. |
| **Bootstrap** | Small committed slices in each dataset's real *format*, with every value invented. Never a real dataset file, never a full download in tests. |
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
    healthcheck: { test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U usher"] }
```

Illustrative and abbreviated, not literal — the real, verified `compose.yml`
(M1 Task 13) also gives `usher` its own healthcheck and has more to say
about the `-h 127.0.0.1` above: without it, `pg_isready` defaults to a Unix
socket, which reaches `pgvector/pgvector:pg17`'s own *temporary* bootstrap
server on a fresh volume and reports ready roughly a second before the
real server is (verified directly, including against a false-positive
window reproduced twice). `./data/models` is not yet mounted by the actual
M1 `compose.yml` — nothing before M6 (embeddings) writes there.

- Alembic migrations run on startup; the app refuses to serve on a schema
  mismatch rather than guessing.
- First run detects an empty catalog and offers bootstrap through the admin API
  — it does not start a multi-hour download unprompted.
- Bootstrap is resumable and checkpointed; a restart mid-import continues.
- **The operator trigger is `usher` (also `python -m usher`), and it exists
  before the HTTP surface does.** `bootstrap` / `bootstrap-status` (M2) and
  `sync` / `sync-status` / `unmatched` / `work` (M4) are the CLI composition
  root, documented command by command in `README.md`;
  [07](07-client-api.md)'s `POST /admin/sources/{id}/sync` and the two
  `/admin/unmatched` routes are M9's and are built over the same services.
  Every one of them has to work against an *empty* database — a command an
  operator can only run after a successful sync is no use for diagnosing
  why the sync did not happen.
- **`--allow-full-retraction` is the only way past ADR-0015's ceiling**, and
  it is a flag rather than a configuration default because it is the one
  input that can mark a whole library unavailable.

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
