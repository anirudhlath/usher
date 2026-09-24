# 08 — Operations

## Configuration

Three layers, split by what changes and when:

| Layer | Holds | Changes |
|---|---|---|
| **Environment** | `DATABASE_URL`, port, log level, embedding model, `USHER_SECRET_KEY`, TMDb key, the LLM endpoint, model and key | Deploy time |
| **Config file** (TOML) | Rate limits, TTLs, enrichment tier. **Not the image cache ladder**, which is a code constant | Restart |
| **Database** | Sources, users, row provider enable/disable | Runtime, via admin API |

Sources are added through the admin API, with no compose edit and no restart.

Until the TOML layer exists, everything in the first two rows is an environment
setting on `usher.config.Settings`, and every setting is documented in
`.env.example`.

**The embedder and the LLM have separate endpoints**: `USHER_EMBEDDING_BASE_URL`
and `USHER_LLM_BASE_URL`.

**`USHER_QUERY_EXPANSION_ENABLED` is a second switch over the LLM client**, off
by default even when `USHER_LLM_ENABLED` is on. Setting it true with no client
is **refused at startup**.

**`USHER_CURATION_POOL_SIZE` is a setting**; size it to the context window of
the model `USHER_LLM_BASE_URL` names.

🔶 **`USHER_LLM_MAX_OUTPUT_TOKENS` shares that context window, and nothing
couples the two.** The endpoint requires `prompt_tokens + llm_max_output_tokens
≤ max_model_len`, so raising the output ceiling lowers the workable pool, and
the failure arrives as a parked job rather than as a startup refusal: no setting
knows `max_model_len`.

Two things are **not** settings:

- **Concurrency per lane.** `USHER_JOB_CONCURRENCY` is the worker's global
  ceiling; the per-kind ceilings under it are fixed in code. The row build is
  always sequential ([01](01-architecture.md)).
- **Row weights and row provider scores.** A weight change is a code change
  plus `usher similar --rebuild`; Continue Watching is always ranked first
  ([06](06-rows-and-recommendations.md)).

**Row providers can be enabled and disabled at runtime.**
`PUT /admin/rows/providers/{slug}` writes `row_provider_settings`, which every
composer reads. A provider with no row there is enabled.

### `.env` has two readers, and that is what the `USHER_COMPOSE_` namespace is for

Docker Compose reads `.env` to substitute `${...}` into `compose.yml`, and
Usher reads the same file as a settings source. `USHER_COMPOSE_*` keys belong
to `compose.yml` and Usher drops them; every other `USHER_*` key must be a
setting, and an unknown one (`USHER_LOG_LEVL=DEBUG`) is a startup failure.
Compose's own `COMPOSE_*` variables are dropped too, so `.env` can carry
`COMPOSE_PROJECT_NAME` (a second stack on one host) and `COMPOSE_FILE` (the
telemetry network, [10](10-telemetry-and-dashboards.md#where-the-stack-lives)).
Any other unknown key is refused.

### A documented setting has to reach the container

`compose.yml` gives the `usher` service the whole `.env` (`env_file:`).
`environment:` overrides it only for what the compose topology owns:
`USHER_DATABASE_URL`, `USHER_HOST`/`USHER_PORT`, and the two bind-mount paths
`USHER_IMAGE_CACHE_DIR`/`USHER_BULK_DATA_DIR`. `USHER_SECRET_KEY` is listed
there too, as `${...:?}`, so a missing key fails at `docker compose up` with a
sentence; its value is still the operator's.

### Starting the app is not a command to walk your library

With `USHER_PUSH_ENABLED=true` the server starts a push lane per enabled
source, and the lane closes its reconnect gap with a delta walk. A delta
resumes from the newest **completed** item walk, so on a source that has never
completed one it would read every item the source has.

**The lane closes gaps, and the first walk is a command.**
`USHER_PUSH_GAP_CLOSE` is a closed vocabulary, defaulting to a refusal:

| Value | The lane does |
|---|---|
| **`cursored`** (default) | Closes a gap that has a cursor. With none, logs a `WARNING` naming the source and pointing at `usher sync`, and walks nothing |
| `always` | Walks when there is no cursor, logging a `WARNING` *before* it starts. The walk still passes through `USHER_PUSH_GAP_MAX_ITEMS`; `USHER_PUSH_GAP_MAX_ITEMS=0` alongside is what restores an unbounded gap close |
| `never` | No gap-closing walk at all, logged at `INFO`. **This has a cost**: Emby does not re-deliver what a disconnected client missed, so a change made during an outage waits for the next `usher sync` |

A brand-new source does not populate itself through the push lane: run
`usher sync --source "<name>"` once, as the
[command-line guide](../guide/command-line.md#syncing-a-media-server) documents.

**A commanded walk is never gated by this.** `usher sync`,
`POST /admin/sources/{id}/sync` and a cron entry all walk regardless of
`USHER_PUSH_GAP_CLOSE`.

### Secrets

Source credentials are **encrypted at rest in Postgres**, using a key supplied
via `USHER_SECRET_KEY`. `Source.credentials_ref` points at the encrypted row;
the plaintext exists only in memory in the adapter that needs it.

- **Credentials are never returned by any API, including admin. Write-only.**
- **Credentials are never logged**, including in error paths and request dumps.
- **A rejected request never echoes the body it rejected.** No validation error
  carries the submitted values.
- **And neither does a rejected *setting*.** An operator learns which setting
  was wrong and never sees the value, with or without `--traceback`.
- ⚠️ **Changing `USHER_SECRET_KEY` without `usher rotate-secret` makes every
  stored credential unreadable** until an operator re-enters it.
  `GET /admin/sources/{id}/status` reports such a source as unreachable and
  unauthenticated, with a fixed re-enter-your-credentials detail.
- **No credential ever reaches a client, with one documented exception**: a
  `direct` playback target's URL carries the source's session token. It does
  not carry Usher's own `DeviceId`. The playback ticket keeps the token out of
  response bodies — it appears only in the `302`'s `Location` header, behind an
  opaque, short-lived ticket — but it is not minted per request, so the grant
  outlives the response that carried it. The token is never logged, never a
  span attribute, and never written to a table, a cache or a file.
- **`llm_api_key` travels in an `Authorization: Bearer` header, never in a
  URL**, and no LLM error message carries a URL or a request body.
- **At `USHER_LOG_LEVEL=DEBUG` the push socket library's own handshake and
  frame logging stays off**, so the source token in its request line is never
  logged.

### Rotation — `usher rotate-secret`

```bash
export USHER_NEW_SECRET_KEY=$(openssl rand -hex 32)   # export, never .env
uv run usher rotate-secret --new-key-env USHER_NEW_SECRET_KEY
# then set USHER_SECRET_KEY to that value and restart
```

**What the key protects**: the `{username, password}` blob in
`source_credentials`, and the playback URL inside a ticket that is **never
persisted**. Rotation therefore rewrites **one table**, at one row per
configured source.

⚠️ **Rotating invalidates every outstanding playback ticket.** A client meets
that as `404 ticket_invalid` and answers by asking `/play` again. Keyset
cursors are not signed with this key.

**The new key is read from the environment, never from an argument.**
`--new-key-env` takes the *name* of an exported variable. `--new-key` is
refused, and no refusal prints the key back. A well-formed variable name that is
unset is echoed, so an operator who forgot the `export` sees which variable was
looked for.

⚠️ **Export it; do not add it to `.env`.** An exported `USHER_NEW_SECRET_KEY`
is invisible to `Settings`; the same name written into `.env` makes every entry
point fail `extra="forbid"` with the new key rendered in the `ValidationError`.

**The new key is validated by `Settings`' own rules before the first row is
touched.**

**Rotation commits per row**, so an interrupted run leaves a **mixed** state
that is recoverable:

- **It is diagnosable**, through the same unreadable-credential state the status
  route already reports.
- **Re-running is the recovery.** A second run finishes a half-rotated table,
  and a run over a fully-rotated one is a no-op.
- **A row that opens under neither key is refused, named and counted**, the run
  exits non-zero, and the row is left exactly as it was.
- 🔴 **A run that refused *every* row is a different diagnosis and says so.** An
  operator who changes `.env` before running the command makes both ciphers the
  same, so every row refuses. That report names `USHER_SECRET_KEY`, says
  nothing was lost, and does not mention re-registration;
  [`docs/runbooks/rotation.md`](../runbooks/rotation.md) is the operator-facing
  form.

## Failure and degradation

**A degraded subsystem narrows functionality; it never fails a request that
local state can answer.**

| Failure | Behaviour |
|---|---|
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong |
| A refused availability sweep | The sweep retracts **nothing** and the run records `FAILED` with the two numbers and the ceiling; the catalog is unaffected. `usher sync` **exits non-zero** on any failed run and names `usher sync --allow-full-retraction`, which is the operator's action *if the removal was intended*. `usher.sync.retraction.fraction{outcome="refused"}` is recorded on every finished full walk, so a flat zero means "nothing was shed". ⚠️ On a *view* of somebody else's library a refusal can be the steady state, tripped first by Usher's own partial coverage rather than the owner's deletions — check that the last full walk **completed** before reaching for the flag |
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. Catalog unaffected |
| Push socket drops | Backoff reconnect; delta reconcile on reconnect, **only when a completed walk gives that delta a cursor** (`USHER_PUSH_GAP_CLOSE`). The same refusal covers a push event deferred to a delta. After `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` (default 5) mark `supports_push = false` and lean on the full walk. **The failure counter resets on delivery, not on connection.** When the ceiling is reached the lane stops, the socket is closed, and the status route answers `push_available: null`. The lane is **not** restarted |
| Gap-closing delta larger than `USHER_PUSH_GAP_MAX_ITEMS` | The item walk stops at the ceiling (default **20,000 items**; `0` is unlimited) and the run records **`FAILED`**, never `COMPLETED`. **Nothing the walk saw is lost**; the cursor does not advance. One WARNING names the source, how far it got, the setting and `usher sync --kind full`. `usher sync --kind delta` and `POST /admin/sources/{id}/sync` are unbounded. **The item lane only** |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades |
| Provider image CDN unreachable | Catalog and every rendered card unaffected — an artwork reference is a row, not a fetch. A **cold** image answers `503 source_unavailable` with `Retry-After`; a **cached** one still serves. Artwork this deployment declines to carry (an `image/svg+xml` logo) is an ordinary `404 not_found`; everything else — a 4xx, an oversized body, a captive portal's HTML under a 200 — is `503 source_unavailable` with **no** `Retry-After`. A CDN 429 or 401/403 answers `503 source_unavailable` on every route, with no `Retry-After` on the refused credential and one on the rate limit. **`Retry-After`'s presence and absence is the contract between the two 503s** |
| LLM call fails | Previous curated rows persist. Home composes without them. The failure is fatal to the *job* and never to the screen. **Only the failures that translate to `PortDataMalformed` park the job** — a 4xx that is none of 429, 401/403 or 408; a 200 whose body does not conform; and a generation that validated to zero rows. The other three families back off with jittered retry |
| LLM call fails during a **search** (query expansion) | The search runs on the query the user typed and `expanded_query` is absent. **The attempt is still billed**, so the warning arrives after the money. Off by default |
| Embedder unavailable, or refusing the model it is serving | A `fused` search falls back to full-text, flagged through `requested_mode` ≠ `mode`. A `semantic` search fails, having no lane left. `INDEX` jobs back off while the endpoint is down, and park while its vectors fail the width or norm check |
| Meilisearch down (if enabled) | Fall back to the Postgres index. It is never the only index |
| Worker in its own process | Every SSE frame a *job* raises is dropped and no client is told; the bus is in-process. **Nothing durable is lost** — the catalog, `import_runs` and `sync_runs` are written either way, so the status routes report the same thing in both topologies. The `usher work` daemon survives a crashed pass and logs it with its traceback; ⚠️ `usher work --once` exits non-zero on one, so a cron entry sees it |
| Postgres down | Total outage. The one hard dependency |

## Job reliability

A Postgres-backed queue.

- **Exponential backoff with equal jitter**: a uniform draw from
  `[base/2, base) × 2^attempts`, so a failed job is never instantly
  re-claimable. `job_backoff_seconds` is the base.
- **A server-supplied `Retry-After` is a floor added to that jittered delay,
  never a replacement for it**; one already in the past counts as zero. **No
  ceiling is imposed on the hint** — a hostile upstream can ask for an
  arbitrarily long wait, bounded only by the attempt ceiling and visible as
  `usher.jobs.queued` failing to drain.
- **Malformed data does not back off at all — it parks on the first attempt.**
  `PortDataMalformed` means the upstream answered and the answer was wrong.
- **Poison threshold** — after `job_max_attempts` attempts a job is *parked*
  with its error, not retried forever and not silently dropped.
- **Work that has become impossible *completes*, and does not park.** A job
  naming an item its source has since deleted, or one no configured source
  addresses, completes. A job whose *key* is unparseable parks.
- **Re-enqueueing does not un-park**, and a parked job's priority is not
  promoted behind a human's back.
- **Re-enqueueing work that has not changed writes nothing**; only a genuine
  priority promotion updates the row.
- ⚠️ **A backed-off job is still `pending`, and nothing bounds the claim scan
  over them.** A queue whose jobs have all backed off — which happens when an
  upstream is broken — makes every claim walk past them.
- Parked jobs are listed in the admin API and counted in metrics.
- Jobs are idempotent, so redelivery is always safe.
- **Abandoned claims are recovered on a lease.** A claim nobody has touched for
  `USHER_JOB_LEASE_SECONDS` (default 300) is taken back, including a dead peer's.
  Every job in flight heartbeats each third of a lease, so the lease bounds
  *"the process stopped"* rather than how long a job may take.
- **Head-of-line blocking.** Jobs run in a bounded pool, but claims are ordered
  `priority DESC, created_at`, so a bulk enqueue at one priority defers
  everything enqueued after it. `usher sync` and `usher bootstrap` run one off
  the queue, in a second process.

## Observability

loguru for logs, OpenTelemetry for metrics and traces, and Grafana over three
datasources. Instrumentation, the metric catalogue, dashboards and alerts are
specified in [10](10-telemetry-and-dashboards.md).

Telemetry is optional: with no OTLP endpoint configured no exporter object is
constructed at all and Usher runs normally.

`GET /health` is liveness; `GET /health/ready` reports Postgres and migration
state — and **gates its status code on those two alone**. Lane state is
reported in the body (`lanes.push`, `lanes.worker`, `lanes.crashed_sources`,
`lanes.recovered_claims`, `lanes.recovered_at`) and per-source push health at
`GET /admin/sources/{id}/status`, never in the code. An unreachable source
never takes the process out of a load balancer.

A dashboard reading the body can tell "down" from "running without Emby".

Readiness makes **no upstream request at all**. The on-demand probe that
*does* open a socket is `usher push --probe`, and it reports what arrived
rather than that the handshake succeeded.

## Testing

| Layer | Approach |
|---|---|
| **Unit** | Services against port fakes. No network, ever |
| **Integration** | Real Postgres (testcontainers). Provider payloads committed as fixtures — *shape*-recorded and value-synthetic, never a capture; never live API calls in CI |
| **Adapter contract suite** | One parametrised test class every `SourceAdapter` must pass |
| **Bootstrap** | Small committed slices in each dataset's real *format*, with every value invented. Never a real dataset file, never a full download in tests |
| **API** | Schema-validated request/response round-trips against the OpenAPI contract |

A new source adapter must pass the same contract suite the Emby adapter passes.

Development follows TDD — failing test first, then implementation.

## Deployment

```yaml
services:
  usher:
    build: .
    env_file: [{ path: .env, required: false }]
    environment:   # only what the topology owns -- this wins over env_file
      USHER_DATABASE_URL: postgresql+asyncpg://usher:usher@postgres:5432/usher
      USHER_HOST: 0.0.0.0
      USHER_PORT: "8000"
      USHER_SECRET_KEY: ${USHER_SECRET_KEY:?set it in .env}
    volumes: ["./data/images:/data/images", "./data/models:/data/models"]
    depends_on: { postgres: { condition: service_healthy } }
  postgres:
    image: pgvector/pgvector:pg17
    volumes: ["./data/postgres:/var/lib/postgresql/data"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U usher"] }
```

Abbreviated, not literal. The Postgres healthcheck's `-h 127.0.0.1` and the
`env_file`/`environment` split **are** normative.

- Alembic migrations run on startup; the app refuses to serve on a schema
  mismatch rather than guessing.
- First run detects an empty catalog and offers bootstrap through the admin API
  — it does not start a multi-hour download unprompted.
- Bootstrap is resumable and checkpointed; a restart mid-import continues.
- **The operator's tool is the `usher` CLI (also `python -m usher`)**;
  `uv run usher --help` lists every subcommand. **Every one works against an
  *empty* database.**
- **A command whose only job needs a subsystem this deployment does not have
  says so and exits 1** — `usher curate` with `USHER_LLM_ENABLED=false`, for
  one.
- **`--allow-full-retraction` is the only way past the retraction ceiling.**
  It is a flag, never a setting: it can mark a whole library unavailable.
- **A failure the operator can fix is one line and exit 1; any other failure
  prints its full traceback.** The one-line families are OS and network errors,
  database driver errors, settings validation, and an upstream that is
  unavailable, refused authentication or rate-limited.
  `usher --traceback <command>` re-raises. Ctrl-C exits 130.

### Scheduled work — two named jobs, no table, and off by default

Two jobs are registered, each with a name and a period — no crontab
expression, no calendar, no timezone, no dependency graph:

| job | what it runs | `last_done()` reads | period |
|---|---|---|---|
| `search_queries` retention | `DELETE FROM search_queries WHERE at < :cutoff`, chunked, a commit per chunk | `min(min(search_queries.at) + window, now)` | 1 day |
| the neighbour rebuild | `usher similar --rebuild`'s batch, `resume=True` | `min(title_neighbors.computed_at)` | `USHER_SIMILAR_REBUILD_PERIOD_HOURS`, 24 h |

**Retention's period is how much expired data may accumulate, not how long a
row is kept.** The window is `USHER_SEARCH_QUERY_RETENTION_DAYS`. An empty
table counts as done.

**Both jobs converge after a failure.** An interrupted prune keeps the chunks it
committed. The registered rebuild always resumes (an operator types
`usher similar --rebuild --resume`), and a seed it cannot clear is re-attempted
once per run rather than looped on.

🔴 **The registered rebuild refuses to run against a table whose vectors were
written by a model this deployment is not configured with**, logs both names at
`ERROR` and reports nothing as done. An operator typing
`usher similar --rebuild` is not refused.

**`USHER_SCHEDULER_ENABLED` defaults to `false`.** It turns the scheduler on
in the server and in bare `usher push`, which runs the server's lanes; `usher
schedule` runs it whatever the setting says. Nothing stops two processes
starting the same job at once.

**A period is a minimum interval since last completion, not a wall-clock
schedule.** For a wall-clock time, or for the jobs without a long-lived
process, run `usher schedule --once` from cron.

⚠️ **A scheduler that is on does not make the neighbour table complete.**
Titles embedded after the last walk started have no `title_neighbors` row until
the next one. **`usher similar` with no arguments shows what is known** — the
oldest row's timestamp with its age, and the count of rows carrying some other
blend.

### Backup — the asymmetry is the point

Every table in the live schema is classified in
`src/usher/db/backup_manifest.py`, with the command that reproduces each
rebuildable one.

| Rebuildable from importers | Precious |
|---|---|
| Catalog, embeddings, search index, neighbour tables, cached images, curated rows, the payload cache, the genome, the job queue, the run logs | **Watch state**, users, source config, `source_credentials`, `llm_calls`, `row_provider_settings`, `search_queries` |

**`media_items` is in neither column.** A backup carries its `title_id` and
`episode_id` links alone — every other column is rebuilt by the next source
walk — and it carries all of them, automatic matches as well as an operator's.
Restore writes a link only where the target's is `NULL`.

**A backup carries natural keys, not ids**: `imdb_id`, falling back to
`(kind, tmdb_id)` and then to the raw UUID, which is accepted **only** where the
target already holds a title with that exact id — so restoring into the same
database needs no second mode. An unresolved reference is refused by name:
`watch_states` and `media_items` refuse the row and count it,
`search_queries.clicked_title_id` is set `NULL` and counted. `curated_rows` is
never carried.

**The command is `usher backup`.** Disaster recovery is a short restore plus a
background rebuild — "lost an afternoon of indexing", not "lost everything".

**The artifact is gzip-compressed JSON Lines: a header object, then one object
per row carrying `table` and `row`.**

- **Every reference is written as a natural key.**
- ⚠️ **The writer holds every carried table in memory before writing a
  byte.** The carried set is small.
- An operator can read it.
- It survives a Postgres version change.

**The destination is written through a scratch sibling and renamed into
place**, so a run that fails part-way leaves the previous artifact intact. The
guarantee is against a failed run and not against a power cut.

**The header carries two stamps and only one is enforced by refusal.**
`schema_revision` is the database's Alembic head, compared by the same check
`/health/ready` uses. `generated_at`, `manifest_version`, `usher_version` and
the per-table row counts are provenance, and the counts let a restore detect a
truncated file.

⚠️ **`source_credentials` is carried as ciphertext and `usher backup` does not
decrypt it**, so an artifact restored into a deployment holding a different
`USHER_SECRET_KEY` restores credentials nobody can read. The command says so on
every run rather than behind a flag. **Keep `USHER_SECRET_KEY` with the
artifact.**

#### Restore — one transaction, four refusals

`usher restore <artifact> [--dry-run]`.

⚠️ **Its normal path is not an empty database.** Into an empty catalog every
watch-state row fails its foreign key. What ships is **a short restore plus a
background rebuild**: the importers rebuild the catalog (`usher bootstrap
--phase all`, then `usher sync`, then `usher work`), and restore lands the
precious rows on top of it.

The operator-facing sequence is
[`docs/runbooks/restore.md`](../runbooks/restore.md) and its clock is
[`docs/runbooks/disaster-recovery.md`](../runbooks/disaster-recovery.md). **All
four runbooks this section asks for — restore, upgrade, disaster recovery and
rotation — are indexed at
[`docs/runbooks/README.md`](../runbooks/README.md).**

**Four refusals, in order, all before any write, and only the fourth is a
report.** (1) an artifact that is not readable or is truncated, including a
gzip member that ends early; (2) a **schema mismatch**, comparing the header's
stamp against the *database's* revision and naming both values; (3) a `table`
key the manifest does not classify, which is what an artifact from a later
schema looks like; (4) **unresolved references, collected across the whole
file and reported together**.

**One transaction for the whole file**, so an unresolved reference in the last
row rolls back the first. ⚠️ **The failure mode of a very large artifact is
memory.** `--dry-run` resolves everything, prints the identical report and
commits nothing.

**The report separates written, already present, nothing-to-write-onto,
skipped-as-unresolvable, and refused**, per table, with the refused list naming
the keys that were looked for. A run with any refusal exits non-zero.

⚠️ **"Already present" and "nothing to write onto" are opposite
instructions.** Both write nothing for `media_items`, but "nothing to write
onto" means the source walk has not yet created the row — the **normal** state
of a first restore into a rebuilt deployment.

**The refused list is capped at 20 named rows with an exact tail.** The
per-table counts are computed from the whole list and stay exact whatever is
printed.

**The header's per-table row counts are a truncation gate**, compared against
the body before any write, so a hand-edit that drops a line is refused.

#### `--skip-unresolvable`

A correctly rebuilt catalog can refuse a whole artifact over links to stubs
carrying neither an `imdb_id` nor a `tmdb_id` — rolling back the household, the
source, its credential, every resolved watch state and every search query with
it.

**`usher restore --skip-unresolvable` drops those rows and commits the rest.
The default is unchanged.** The dropped rows are counted in a bucket of their
own. It composes with `--dry-run`.

⚠️ **It covers exactly the references the importers rebuild.** A household the
target does not hold, a source colliding on a name, and a credential whose
source is absent all still refuse with the flag set.

**How each carried table is restored:**

| table | rule |
|---|---|
| `users` | insert if the name is absent (`uq_users_name`); otherwise every reference adopts the id the target already holds |
| `sources` | insert if the id is absent; **refuse** if a *different* source holds that name |
| `source_credentials` | insert on `ref`, `DO NOTHING`; refuse if its source is not here |
| `watch_states` | upsert on `uq_watch_states_user_title` / `uq_watch_states_user_episode`, whichever the row's target names |
| `llm_calls` | insert on `id`, `DO NOTHING` — append-only, so restoring one spend ledger twice is safe |
| `row_provider_settings` | upsert on `slug_prefix` |
| `search_queries` | insert on `id`, `DO NOTHING`, `clicked_title_id` nulled where unresolved |
| `media_items` | update the two links **only where the target's `title_id` is `NULL`** |

**Restoring the same artifact twice is a no-op on the second run** — every
table's count identical.

**What "rebuildable" means, table by table, where it is not obvious:**

| Table | Rebuildable? | From what, at what cost |
|---|---|---|
| `people`, `credits`, `collections` | **yes, with no network call at all** | `raw_payloads`, via `usher derive --backfill`. The payload cache is the backup |
| `user_taste` | **yes** | a mean over embeddings of the household's watch states; a missing row is recomputed exactly like a stale one |
| `title_neighbors` | **yes** | `usher similar --rebuild`; `blend_fingerprint` tells a restored table from a current one |
| `genome_scores`, `genome_tags` | **yes, but only from upstream** | re-download `ml-latest.zip` and re-run `bootstrap --phase movielens`. **Not guaranteed**: GroupLens can withdraw or replace the archive, and then it is not rebuildable at all. They share a `genome_revision` and are restored together or not at all |
| `curated_rows` | **yes, and cheaply — but not to the same rows** | one completion per household. "Rebuildable" means *a screen appears*, not *the screen comes back* |
| `raw_payloads` | **yes, at the price of the whole crawl** | not carried: it is third-party payloads verbatim, which rule 1 of [04](04-catalog-bootstrap.md) forbids redistributing. There is no `usher backup --include-payloads` |
| **`llm_calls`** | **NO. From nothing.** | It is a **spend ledger**, and the only record that money was spent. Nothing recomputes it — not the catalog, not `curated_rows`, not the provider |

**`search_queries` is precious too.** It is the record of what a household
typed and what it then played, and nothing re-derives it.

### Resource envelope

**These are sizing estimates for an operator provisioning a disk**, read on
2026-09-24 from a 1,277,145-title catalog with 137,467 titles (11%) enriched, at
migration `m10b`. Nothing reads them, no host enforces them, and no threshold or
policy is derived from them.

| | |
|---|---|
| Postgres, catalog + indexes | **~8.6 GB** (`pg_database_size` 8,648,103,603 B), and the enriched fraction is what moves it: `raw_payloads` is 1,226 MB of that, `title_embeddings` 772 MB, `title_neighbors` 793 MB. A fully-enriched catalog is several times larger |
| Postgres, + `titles.credit_names` | **+624 MB settled, +1,368 MB transient** before a vacuum — the peak is what an operator's disk sees |
| A migration that rewrites a large column | Budget the **peak**: a rewrite leaves a dead tuple per live row until a vacuum, and a migration runs no `VACUUM`. `m09d`'s rewrite of `credits` took it from **794 MB to 1,431 MB** (+637 MB transient), settling at **740 MB** only after a `VACUUM FULL` |
| HNSW (`halfvec`) | 🔶 At 1024 lanes, **376 MB** of index inside the 772 MB `title_embeddings` relation over 137,375 embeddings; **~3.5 GB** extrapolated to full catalog coverage |
| Image cache | **Bounded per image and unbounded over time.** At most four entries an image; **nothing evicts**, so the size is `images browsed × up to four rungs × their bytes` and **the growth driver is browse coverage, not catalog size**. ⚠️ There is no LRU ceiling to configure and no eviction method to call: the four real settings are `image_cache_dir` (where), `image_max_bytes` (a **per-image** 5 MiB refusal, never a cache cap), `image_fetch_timeout_seconds` and `image_cdn_base_url`. Extrapolated: **~225 GB** at one image per title, **~668 GB** at all four rungs. This directory is half of [10](10-telemetry-and-dashboards.md)'s *Disk projection* alert |
| Postgres, largest relations | `titles` **2,278 MB**, `credits` **1,237 MB**, `raw_payloads` **1,226 MB**, `title_neighbors` **793 MB** — `pg_total_relation_size` at the reading above |
| Usher process | **~500 MB–1 GB**, plus the loaded model under the `fastembed:` runtime — 🔶 unmeasured, and no smaller than the checkpoint below. Under `openai:` no model is loaded in this process at all |
| Embedding model | 🔶 **~1.2 GB** on disk for the shipped default, `fastembed:BAAI/bge-large-en-v1.5` — fastembed's declared size, not a measured download; **0 on the `openai:` runtime** |

**A full `usher similar --rebuild` is an overnight job, not a follow-on step.**

Tuning that matters: `maintenance_work_mem` high enough to avoid the
`hnsw graph no longer fits into maintenance_work_mem` notice during index
builds, `max_parallel_maintenance_workers = 7`, and GIN `fastupdate = off`.
