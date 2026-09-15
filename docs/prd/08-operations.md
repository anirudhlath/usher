# 08 — Operations

## Configuration

Three layers, split by what changes and when:

| Layer | Holds | Changes |
|---|---|---|
| **Environment** | `DATABASE_URL`, port, log level, embedding model, `USHER_SECRET_KEY`, TMDb key, the LLM endpoint, model and key | Deploy time |
| **Config file** (TOML) | Rate limits, TTLs, enrichment tier. **Not the image cache ladder**, which is a code constant | Restart |
| **Database** | Sources, users, row provider enable/disable | Runtime, via admin API |

Sources live in the database because they are added through the admin API. A
deployment that needs a compose edit and a restart to connect a media server is
the wrong shape for this.

Until the TOML layer exists, everything in the first two rows is an environment
setting on `usher.config.Settings` and is documented in `.env.example` —
completeness in both directions, so a setting an operator cannot discover and a
documented key that is not a setting are both test failures
(`tests/unit/test_deployment_config.py`).

⚠️ **`USHER_EMBEDDING_BASE_URL` is deliberately not `USHER_LLM_BASE_URL`.** They
are one endpoint on many hosted providers and two processes here, because vLLM
serves one model per process, so collapsing them would make "point the embedder
somewhere else" impossible without moving curation too.

**`USHER_QUERY_EXPANSION_ENABLED` is the one place this project ships two
switches over one dependency.** `USHER_LLM_ENABLED` builds the client, and
query expansion is off even when it is on, because the retrieval measurement in
[05](05-search-and-similarity.md) put expansion's effect the wrong way round.
Setting it true with no client is **refused at startup** rather than ignored,
which is this document's dead-config rule applied to a *state* rather than to a
typo.

**`USHER_CURATION_POOL_SIZE` is a setting and the row weights are not.** The
pool is assembled, sent and discarded, so there is no half-computed artefact,
and what the number is really about is the context window of whatever model
`USHER_LLM_BASE_URL` names — a deployment fact an operator must be able to
change without editing code.

🔶 **Its sibling is `USHER_LLM_MAX_OUTPUT_TOKENS`, and nothing couples them.**
The endpoint's constraint is `prompt_tokens + llm_max_output_tokens ≤
max_model_len`, so raising the output ceiling silently lowers the workable pool
and the failure arrives as a parked job rather than as a startup refusal. That
is the one place the refuse-an-impossible-state rule is not applied, because
`max_model_len` is a property of the endpoint that no setting knows.

Two entries will **not** become settings:

- **Concurrency per lane.** A setting cannot be added ahead of the mechanism it
  would bound. `USHER_JOB_CONCURRENCY` is the worker's global ceiling; the
  per-kind ceilings under it are code (`usher.services.jobs.KIND_CONCURRENCY`),
  one entry per `JobKind` with its measurement beside it. The *row build* still
  has no such setting, because its mechanism is a `for` loop whose correct
  value is 1 ([01](01-architecture.md)).
- **Row weights.** Changing a weight changes what "similar" and "relevant"
  *mean*, and every row already written to `title_neighbors` was written under
  the old meaning, so an operator turning a dial silently gets a table half
  computed one way and half the other. A weight change is a code change and a
  rebuild. **Row provider scores are the same answer for different reasons**: a
  configurable score set can reorder Continue Watching, which
  [06](06-rows-and-recommendations.md) fixes as *"always ranked first"*, and a
  score only decides ordering among proposals, after which diversity
  constraints and the top-N cap reshape the result.

**Row provider enable/disable is a control that should exist, and does.**
`row_provider_settings(slug_prefix PK, enabled, updated_at)` is written by
`PUT /admin/rows/providers/{slug}` and read by every composer. It is created
**empty and never seeded**: absence means enabled, exactly as "enabled by
registration in code" already meant, so there is no state where the table
exists and says nothing, and no migration carrying a second copy of the
registry.

### `.env` has two readers, and that is what the `USHER_COMPOSE_` namespace is for

Docker Compose reads `.env` to substitute `${...}` into `compose.yml`;
pydantic-settings reads the same file as a settings source with
`extra="forbid"`. The two vocabularies overlap, so a variable meaningful only
to compose is an *extra* input to `Settings` — and one such key made
`cp .env.example .env`, the documented first step, fail every entry point.

`extra="forbid"` stays, because it is what turns `USHER_LOG_LEVL=DEBUG` into a
startup failure instead of a line in `.env` that silently does nothing. The two
readings are separated by **name** instead: `USHER_COMPOSE_*` belongs to
`compose.yml` and the application drops it before validation; every other
`USHER_*` key is a setting or a typo.

### A documented setting has to reach the container

`compose.yml` gives the `usher` service the whole `.env` (`env_file:`), not a
hand-maintained `environment:` list. The list form forwarded 5 of 30 documented
keys, so 24 were documented, worked when delivered directly, and were silently
ignored when set where the docs point. **A setting that is documented but
unreachable is dead config that looks like a control.**

`environment:` still wins over `env_file:`, so what is left in it is exactly
what the compose *topology* owns rather than the operator:
`USHER_DATABASE_URL`, `USHER_HOST`/`USHER_PORT`, `USHER_SECRET_KEY`
(substituted as `${...:?}` so a missing key fails at `docker compose up` with a
sentence) and `USHER_IMAGE_CACHE_DIR` (a bind-mount path).

### Starting the app is not a command to walk your library

With `USHER_PUSH_ENABLED=true` the server starts a push lane per enabled
source, and the lane closes its reconnect gap with a delta walk. A delta
resumes from the newest **completed** item walk, so a deployment that has never
completed one reads every item the source has — a walk of a whole library
performed by `uvicorn` with default settings, on a media server the operator
may not own, with no command issued.

**The lane closes gaps, and the first walk is a command.**
`USHER_PUSH_GAP_CLOSE` is a closed vocabulary, defaulting to a refusal:

| Value | The lane does |
|---|---|
| **`cursored`** (default) | Closes a gap that has a cursor. With none, logs a `WARNING` naming the source and pointing at `usher sync`, and walks nothing |
| `always` | Walks when there is no cursor, logging a `WARNING` *before* it starts. The walk still passes through `USHER_PUSH_GAP_MAX_ITEMS`; `USHER_PUSH_GAP_MAX_ITEMS=0` alongside is what restores an unbounded gap close |
| `never` | No gap-closing walk at all, logged at `INFO`. **This has a cost**: Emby does not re-deliver what a disconnected client missed, so a change made during an outage waits for the next `usher sync` |

**What changes for an existing deployment:** only one that has never completed
an item walk for a source. A brand-new source no longer populates itself as a
side effect of the push lane reconnecting — run `usher sync --source "<name>"`
once, which is the step [the README](../../README.md) already documents.

**Neither the rate limit nor a truncation was the answer.**
`USHER_PUSH_GAP_MIN_INTERVAL_SECONDS` bounds how *often* a gap is closed and
says nothing about how large the walk is; and a cap on items would end a run
that then records `COMPLETED`, whose `started_at` becomes the floor for every
later delta.

**A commanded walk is never gated by this.** `usher sync`,
`POST /admin/sources/{id}/sync` and a cron entry all reach `ReconcileService`
directly; the setting governs `LaneSupervisor._close_gap` alone.

### Secrets

Source credentials are **encrypted at rest in Postgres**, using a key supplied
via `USHER_SECRET_KEY`. `Source.credentials_ref` points at the encrypted row;
the plaintext exists only in memory in the adapter that needs it.

- **Credentials are never returned by any API, including admin. Write-only.**
  Structurally, not by discipline: no response DTO in `api/dto/` has a field a
  username or password could be assigned to, enforced over the whole package
  so a response type added later inherits the rule.
- **Credentials are never logged**, including in error paths and request dumps.
- **A rejected request never echoes the body it rejected.** FastAPI's default
  `422` answers with pydantic's errors, and a `missing` error carries the whole
  *unparsed* request dict in its `input` field — every sibling value as
  submitted, before any of them became a `SecretStr`. `usher.api.errors` strips
  `input` from every validation error, app-wide.
- **And neither does a rejected *setting*.** `usher.cli._settings_problem`
  renders `loc` and `msg` and drops `input`, so an operator learns which
  setting was wrong and never sees the value. **`--traceback` does not reopen
  it**: a settings failure's stack is pydantic frames that diagnose nothing, so
  the only thing re-raising would add is the credential.
- **Rotating `USHER_SECRET_KEY` re-encrypts on next write**, and the bulk case
  is `usher rotate-secret` below. ⚠️ "On next write" keeps nothing limping:
  `PostgresCredentialStore.put` encrypts with whatever cipher it was built
  with, so the only re-encrypting write is a credential an operator re-types.
  Until then the old rows are unreadable, and **that state is rendered rather
  than raised**: Fernet's authentication tag makes a wrong key a diagnosable
  `PortDataMalformed`, and `GET /admin/sources/{id}/status` reports it as an
  unreachable, unauthenticated source with a re-enter-your-credentials detail.
  The rendered detail is a fixed string, never the exception's own, which names
  the `credentials_ref`.
- **No credential ever reaches a client.** One documented exception: a `direct`
  playback target's URL carries the source's session token, because Usher never
  proxies the bytes and the route that serves them authenticates. It does not
  carry Usher's own `DeviceId`. M9's playback ticket narrows it — a `302` moves
  the token out of the response body and into a `Location` header, which makes
  the shareable artifact opaque and short-lived rather than removing the grant.
- **The exception reaches the first rule too.** What still binds it without
  exception: never logged (enforced once, on the DTO that carries it, rather
  than by each caller), never a span attribute, and never written to a table, a
  cache or a file. It is not minted per request, so the grant outlives the
  response that carried it.
- **`database_url`, `secret_key`, `tmdb_api_key` and `llm_api_key` are
  `pydantic.SecretStr`**, unwrapped only at the point of use. `llm_api_key` is
  the first credential this project hands to a third party it did not choose,
  so it travels in an `Authorization: Bearer` header and never in a URL — span
  attributes record full URLs — and no exception message in `adapters/llm/`
  carries a URL or a request body, because the body *is* the prompt and the
  prompt carries the household's watch history.
- **"Never logged" has to cover libraries Usher hands a credential to.** The
  source token is the query string of a `websockets` URL, and that client
  debug-logs its own request line, so at `USHER_LOG_LEVEL=DEBUG` the rule was
  broken by code this project does not own. The guard is a logger whose *level*
  is above `CRITICAL`, re-asserted on every connect, and it costs the library's
  own handshake and frame diagnostics.

### Rotation — `usher rotate-secret`

```bash
export USHER_NEW_SECRET_KEY=$(openssl rand -hex 32)   # export, never .env
uv run usher rotate-secret --new-key-env USHER_NEW_SECRET_KEY
# then set USHER_SECRET_KEY to that value and restart
```

**What the key protects.** Two HKDF-SHA256 derivations over `USHER_SECRET_KEY`,
differing only in their `info` string: one encrypts the `{username, password}`
blob in `source_credentials.ciphertext`, the other encrypts a playback target
URL inside a ticket that is **never persisted**. Rotation therefore touches
**one table**, at one row per configured source.

⚠️ **The ticket cipher needs no rotation.** Rotating invalidates every
outstanding one; a client meets that as `404 ticket_invalid` and answers by
asking `/play` again. Keyset cursors are outside this entirely:
`Settings.secret_key` is deliberately not what signs one.

**The new key is read from the environment and never from an argument.** A key
on a command line is in the shell's history file and in `ps` output for every
user on the box. Four controls:

1. **`allow_abbrev=False` on this subparser**, which does not propagate from
   the parser above it — `--new-key` was otherwise an unambiguous *prefix* of
   `--new-key-env` and argparse bound the key into the field meant for a
   variable name.
2. **`--new-key` is declared** as a suppressed tripwire whose only action is to
   refuse, naming the flag and never the value — without it, argparse's own
   `unrecognized arguments` message prints the key.
3. **`parse_args` refuses this one command's unrecognised arguments without
   naming them.** Every other command keeps argparse's wording, because there a
   refused token is a typo and naming it is how it gets fixed.
4. **`--new-key-env`'s value must be an environment variable *name*** and must
   not be something `Settings` would accept as a key; either refusal prints
   nothing back. The second is not redundant: `openssl rand -hex 32` emits a
   legal variable name whenever its first character is `a`–`f`.

A well-formed name that cannot be a key is still echoed when it is unset,
deliberately — an operator who forgot the `export` needs to see which variable
was looked for.

⚠️ **Export it; do not add it to `.env`.** An exported `USHER_NEW_SECRET_KEY`
is invisible to `Settings`; the same name written into `.env` makes every entry
point fail `extra="forbid"` with the new key rendered in the `ValidationError`.

**The new key is validated by `Settings`' own rules before the first row is
touched**, by constructing a real `Settings` rather than by a second copy of
the rules: a rotation to a key `Settings` would refuse bricks the next start.

**Per-row commit, deliberately the opposite of `usher restore`.** One
transaction over N rows means an interrupted rotation leaves every row on the
old key while the operator has already changed theirs. Per-row commit leaves a
**mixed** state, which is recoverable:

- **It is diagnosable**, through the same `PortDataMalformed` the status route
  already renders.
- **Re-running is the recovery, and it needs no ledger.** Every row is tried
  with the **new** cipher first and skipped if it already opens; only then is
  the old one tried. A second run finishes a half-rotated table and a run over
  a fully-rotated one is a no-op. Trying the old cipher first double-encrypts
  every row a previous run moved.
- **A row that opens under neither key is refused, named and counted**, the run
  exits non-zero, and the row is left exactly as it was.
- 🔴 **A run that refused *every* row is a different diagnosis and says so.** An
  operator who changes `.env` before running the command makes both ciphers the
  same, so every row refuses. The saturated arm names `USHER_SECRET_KEY`, says
  nothing was lost, and does not mention re-registration;
  [`docs/runbooks/rotation.md`](../runbooks/rotation.md) is the operator-facing
  form.

**The raw ciphertext is a second port rather than three more methods on
`CredentialStore`**, so only the composition root that builds the rotation
service can name it — and `PostgresCredentialRotationStore` takes no
`secret_key` at all, so it moves bytes it cannot read.

## Failure and degradation

**A degraded subsystem narrows functionality; it never fails a request that
local state can answer.**

| Failure | Behaviour |
|---|---|
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong |
| A refused availability sweep | The sweep retracts **nothing** and the run records `FAILED` with the two numbers and the ceiling; the catalog is unaffected. `usher sync` **exits non-zero** on any failed run and names `usher sync --allow-full-retraction`, which is the operator's action *if the removal was intended*. `usher.sync.retraction.fraction{outcome="refused"}` is recorded on every finished full walk, so a flat zero means "nothing was shed" rather than "no sweep ran". ⚠️ On a *view* of somebody else's library a refusal can be the steady state, and what trips it first is Usher's own partial coverage rather than the owner's deletions — check that the last full walk **completed** before reaching for the flag |
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. Catalog unaffected |
| Push socket drops | Backoff reconnect; delta reconcile on reconnect, **only when a completed walk gives that delta a cursor** (`USHER_PUSH_GAP_CLOSE`). The same refusal covers a push event deferred to a delta. After `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` (default 5) mark `supports_push = false` and lean on the nightly walk. **The failure counter resets on delivery, not on connection**: a proxy that upgrades and then buffers would connect perfectly every time, so a counter reset by connecting would never reach the ceiling. When the ceiling is reached the lane's task finishes and the next `refresh()` **releases the adapter**, so the socket is closed rather than held for the process lifetime and the status route answers `push_available: null` rather than `false` off a dead ledger. The lane is deliberately **not** restarted |
| Gap-closing delta larger than `USHER_PUSH_GAP_MAX_ITEMS` | The item walk stops at the ceiling (default **20,000 items**; `0` is unlimited) and the run records **`FAILED`**, never `COMPLETED`. **Nothing the walk saw is lost** — `_flush` commits per batch — and what a ceiling costs is the cursor advance. One WARNING names the source, how far it got, the setting and `usher sync --kind full`. `usher sync --kind delta` and `POST /admin/sources/{id}/sync` are unbounded: the ceiling is the lane's, and the lane is the caller nobody typed a command for. **The item lane only** |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades |
| Provider image CDN unreachable | Catalog and every rendered card unaffected — an artwork reference is a row, not a fetch. A **cold** image answers `503 source_unavailable` with `Retry-After`; a **cached** one still serves, because `GET /images/{id}` reads the disk before the network. Artwork this deployment declines to carry (an `image/svg+xml` logo) is an ordinary `404 not_found`; everything else — a 4xx, an oversized body, a captive portal's HTML under a 200 — is `503 source_unavailable` with **no** `Retry-After`, since re-asking produces the same answer. A CDN 429 or 401/403 is answered by an exception handler on the *app*, so every route inherits it: `503 source_unavailable` for both, with no `Retry-After` on the refused credential and one on the rate limit. **`Retry-After`'s presence and absence is the contract between the two 503s**, because the closed `code` vocabulary has no member for a 502 |
| LLM call fails | Previous curated rows persist. Home composes without them. The failure is fatal to the *job* and never to the screen. **Only the failures that translate to `PortDataMalformed` park the job** — a 4xx that is none of 429, 401/403 or 408; a 200 whose body does not conform; and a generation that validated to zero rows. The other three families back off with jittered retry |
| LLM call fails during a **search** (query expansion) | The search runs on the query the user typed and `expanded_query` is absent. **The attempt is still billed**, so the warning arrives after the money. Off by default |
| Embedder unavailable | Semantic search falls back to full-text, flagged in the response |
| Meilisearch down (if enabled) | Fall back to the Postgres index. It is never the only index |
| Worker in its own process | Every SSE frame a *job* raises reaches a `NullEventPublisher` and no client is told; the bus is in-process. **Nothing durable is lost** — the catalog, `import_runs` and `sync_runs` are written either way, so the status routes report the same thing in both topologies. Both roots catch a crashed pass and record it with `logger.exception` rather than `str(exc)`. ⚠️ `usher work --once` is deliberately outside that arm: a cron entry reads the exit code, and a guard there would answer a crashed pass with `0` |
| Postgres down | Total outage. The one hard dependency, deliberately |

## Job reliability

Postgres-backed queue, claimed with `SELECT … FOR UPDATE SKIP LOCKED`.

- Exponential backoff with jitter; per-job attempt counter. The jitter is
  **equal jitter** — a uniform draw from `[base/2, base) × 2^attempts` — not
  *full* jitter, whose minimum draw is arbitrarily close to zero, so a share of
  failures against a broken upstream retry effectively immediately. A
  half-interval floor keeps the spread while making "a failed job is not
  instantly re-claimable" a property rather than a probability.
  `job_backoff_seconds` is the base.
- **A server-supplied `Retry-After` is a floor added to that jittered delay,
  never a replacement for it.** It is read off a caught `PortRateLimited` by
  `isinstance`, never by `getattr`, and clamped at zero before it is added,
  because an HTTP-date form can already be in the past and an unclamped hint
  would pull a retry *earlier* than the ordinary schedule. **No ceiling is
  imposed on the hint** — a hostile upstream can ask for an arbitrarily long
  wait, bounded only by the attempt ceiling and visible as `usher.jobs.queued`
  failing to drain.
- **Malformed data does not back off at all — it parks on the first attempt.**
  `PortDataMalformed` means the upstream answered and the answer was wrong.
- **Poison threshold** — after `job_max_attempts` attempts a job is *parked*
  with its error, not retried forever and not silently dropped.
- **Work that has become impossible *completes*, and does not park.** A job
  naming an item its source has since deleted, or one no configured source
  addresses, is not poison: parking is reserved for work a human has to look
  at. A job whose *key* is unparseable does park, because that is a real defect.
- **Re-enqueueing does not un-park**, and a parked job's priority is not
  promoted behind a human's back.
- **Re-enqueueing work that has not changed writes nothing.** The update fires
  only on a genuine promotion (`jobs.priority < excluded.priority`), and
  `enqueue` reports 0 rows written otherwise.
- ⚠️ **A backed-off job is still `pending`, and nothing bounds the claim scan
  over them.** `run_after <= clock_timestamp()` cannot be an indexed predicate,
  so a queue whose jobs have all backed off makes every claim walk past them.
  Recorded rather than solved: putting `run_after` first destroys the priority
  ordering the queue exists for, and the condition only arises when an upstream
  is broken.
- Parked jobs are listed in the admin API and counted in metrics. Silent
  failure is the thing worth engineering against.
- Jobs are idempotent by construction, so redelivery is always safe.
- **Abandoned claims are recovered on a lease.** `JobWorker.recover()` passes
  an explicit `USHER_JOB_LEASE_SECONDS` (default 300), so it takes back only
  claims nobody has touched for a lease and is safe to run repeatedly — which
  is what lets a live worker recover a *dead peer's* orphans rather than only
  its own. `JobQueue.touch()` is the heartbeat, beaten every third of a lease
  for everything in flight, so the lease bounds *"the process stopped"* rather
  than how long a job may take.
- **Head-of-line blocking is accepted and priced.** Jobs run in a bounded pool,
  so a sync no longer makes every other kind unavailable for its duration; what
  survives is the *claim* ordering (`priority DESC, created_at`), so a bulk
  enqueue at one priority still defers everything enqueued after it. The queue
  is chosen anyway for its dedup on `(kind, key)` and its durability across a
  restart, and both long handlers commit per batch so no transaction spans the
  job. `usher sync` and `usher bootstrap` remain the way to run one off the
  queue, at the cost of a second process rather than a second lane.

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
`GET /admin/sources/{id}/status`, never in the code. A readiness probe that
failed because a source was unreachable would take the process out of a load
balancer for a reason restarting it cannot fix; and a process that has just
taken back a dead peer's claims is doing its job, so saying so must not drop it
out of one either.

The report is degraded rather than binary, so a dashboard can distinguish
"down" from "running without Emby" — by reading the body, which is what a
dashboard does and what Kubernetes, Docker `healthcheck` and a load balancer
never do.

Lane state is free to report — `lanes.push` is the set of running lane *tasks*
and `push_available` is an in-memory ledger of messages received, not a probe —
so readiness makes **no upstream request at all**. The on-demand probe that
*does* open a socket is `usher push --probe`, and it reports what arrived
rather than that the handshake succeeded.

## Testing

| Layer | Approach |
|---|---|
| **Unit** | Services against port fakes. No network, ever. Fakes are trivial because ports are ABCs |
| **Integration** | Real Postgres (testcontainers). Provider payloads committed as fixtures — *shape*-recorded and value-synthetic, never a capture; never live API calls in CI |
| **Adapter contract suite** | One parametrised test class every `SourceAdapter` must pass |
| **Bootstrap** | Small committed slices in each dataset's real *format*, with every value invented. Never a real dataset file, never a full download in tests |
| **API** | Schema-validated request/response round-trips against the OpenAPI contract |

**The contract suite is the load-bearing one.** When a Jellyfin adapter is
written it either passes the same tests the Emby adapter passes, or the port
was wrong.

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

Abbreviated, not literal. `-h 127.0.0.1` is load-bearing: without it
`pg_isready` defaults to a Unix socket, which reaches the image's *temporary*
bootstrap server on a fresh volume and reports ready before the real server is.
The `env_file`/`environment` split **is** normative.

- Alembic migrations run on startup; the app refuses to serve on a schema
  mismatch rather than guessing.
- First run detects an empty catalog and offers bootstrap through the admin API
  — it does not start a multi-hour download unprompted.
- Bootstrap is resumable and checkpointed; a restart mid-import continues.
- **The operator trigger is `usher` (also `python -m usher`), and it exists
  before the HTTP surface does.** `serve`, `bootstrap`, `bootstrap-status`,
  `sync`, `sync-status`, `unmatched`, `work`, `push`, `index`, `search`,
  `suggest`, `similar`, `derive`, `home` and `curate` are the CLI composition
  root, documented command by command in `README.md`. **Every one has to work
  against an *empty* database** — a command an operator can only run after a
  successful sync is no use for diagnosing why the sync did not happen.
- **A command whose only job needs a subsystem this deployment does not have
  says so and exits 1.** `usher curate` with `USHER_LLM_ENABLED=false` has no
  client to build; unlike `GET /home` (the screen is shorter) and `usher work`
  (`curate` is left unclaimed), there is nothing to narrow to, and a run that
  printed an empty report and exited 0 would tell a cron entry that curation is
  running.
- **`--allow-full-retraction` is the only way past the retraction ceiling**,
  and it is a flag rather than a configuration default because it is the one
  input that can mark a whole library unavailable.
- **A failure the operator can fix is a message; a failure they cannot is a
  stack.** `main` has a single `try` around the whole dispatch naming the
  families an operator can act on — `OSError`, `DBAPIError`,
  `httpx.HTTPError`, `ValidationError`, and the port taxonomy's transport half
  (`PortUnavailable`, `PortAuthFailed`, `PortRateLimited`) — each answered with
  one line and exit 1; `usher --traceback <command>` re-raises. **`Exception`
  is deliberately not among them**, so a bug still gets its full traceback, and
  **`DBAPIError` rather than `SQLAlchemyError`**, which is also the base of
  `InvalidRequestError` and would answer real bugs with one line and no stack.
  `RepositoryConflict`, `RepositoryNotFound` and `PortDataMalformed` keep their
  stacks, because several of their raise sites are tripwires for bugs in
  Usher's own code. Ctrl-C exits 130.

### Scheduled work — two named jobs, no table, and off by default

Two jobs are registered, and the contract is a **name**, a **period**, a
`last_done()` and a `run()` — no crontab expression, no calendar, no timezone,
no dependency graph:

| job | what it runs | `last_done()` reads | period |
|---|---|---|---|
| `search_queries` retention | `DELETE FROM search_queries WHERE at < :cutoff`, chunked, a commit per chunk | `min(min(search_queries.at) + window, now)` | 1 day |
| the neighbour rebuild | `usher similar --rebuild`'s batch, `resume=True` | `min(title_neighbors.computed_at)` | `USHER_SIMILAR_REBUILD_PERIOD_HOURS`, 24 h |

**A `last_done()` has to be a reading this job's own runs move.** Retention
maintains the table's *lower bound*, so a successful run at *T* establishes
"no row is older than *T* − window" — hence `min(min(at) + window, now)`. A
prune moves it and a new search cannot, because a new row is the newest one. An
**empty** table answers `now` rather than `None`: nothing to prune is the
invariant satisfied, where `None` would mean "never built, therefore due" and
would make an idle deployment prune on every tick forever.

**So the period is how much expired data may accumulate, not how long a row is
kept**, and the two numbers are set in different places on purpose: the
*window* is `USHER_SEARCH_QUERY_RETENTION_DAYS` because it is a household's own
history, and the *period* is a property of the job. The chunk size is what a
first prune after the suggest writer is switched on, or after an outage, needs.

⚠️ **A failed retention run converges and a failed rebuild had to be made to.**
A prune's committed chunks go oldest first and remove rows permanently, so an
interrupted run leaves progress the next one keeps.
`SimilarityService.rebuild(resume=True)` reads a **start cursor** off the
artefact once, before the first page, and then walks forward exactly as it did
— a starting offset computed once and **not** a loop predicate, so a seed the
rebuild cannot clear is re-attempted once per run rather than looped on. The
registration always resumes; an operator types
`usher similar --rebuild --resume`.

🔴 **The registered job refuses to run against a table whose vectors were
written by a model this deployment is not configured with**, logs both names at
`ERROR` and reports nothing as done — otherwise it would spend hours drawing
pools from one embedding space and stamping them with another's fingerprint.
The guard is on the **registration** and deliberately not inside `rebuild`: an
operator typing a command about a table they can see may legitimately want to
force a mid-swap walk; a timer starting a multi-hour one unasked may not.

**`USHER_SCHEDULER_ENABLED` defaults to `false`.** A full rebuild is hours of
work, and there is no mutual exclusion — the exclusion `JobQueue` provides is a
lock on a job **row**, and this component has no rows — so a deployment running
both the server and a separate `usher work` container with the scheduler on in
each would start that walk twice.

**A period is a minimum interval since last completion, not a wall-clock
schedule.** "Every night at 3am" is not expressible; an operator who wants a
wall-clock time runs `usher schedule --once` from their own cron, which is also
the answer for anyone who wants the jobs without a long-lived process.

⚠️ **A scheduler that is on does not make the artefact complete.**
`last_done()` answers *when*, not *whether*: titles embedded after the last
walk started carry no `title_neighbors` row at all, and `stale_neighbors()`
cannot see them because a missing row has no fingerprint to disagree. The
period is what eventually covers a growing population. **`usher similar` with
no arguments is where an operator sees the two facts that exist** — the oldest
row's timestamp with its age, and the count of rows carrying some other blend.

### Backup — the asymmetry is the point

**The split is generated from a manifest and a test enforces it.**
`src/usher/db/backup_manifest.py` classifies **every table in the live
schema**, each with the reason it is where it is and, for a rebuildable one,
the command that reproduces it.
`tests/integration/test_backup_manifest_covers_the_live_schema.py` reads
`information_schema` after the real migration chain and fails in **both**
directions, so a table a future migration adds is a red rather than a paragraph
nobody remembered to update.

| Rebuildable from importers | Precious |
|---|---|
| Catalog, embeddings, search index, neighbour tables, cached images, curated rows, the payload cache, the genome, the job queue, the run logs | **Watch state**, users, source config, `source_credentials`, `llm_calls`, `row_provider_settings`, `search_queries` |

**`media_items` is in neither column.** The manifest gives it a third class,
`PARTIAL`, carrying `title_id` and `episode_id` alone — every other column is
rebuilt by the next source walk. It carries **all** of those links rather than
only the operator's, because the schema has no provenance column and the
automatic match handler calls the same `attach_title` as the route. The harm is
asymmetric: a link the match ladder would have re-derived is re-derived to the
same answer, and a link it would not re-derive is exactly the operator's
judgement. Restore writes one only where the target's is `NULL`.

**A backup carries natural keys, not ids.** `upsert_titles` mints a fresh id
per staged row, so two catalogs built from the same IMDb dump agree on every
natural key and on no id at all. An artifact carries `imdb_id`, falling back to
`(kind, tmdb_id)` and then to the raw UUID — accepted **only** where the target
already holds a title with that exact id, which is what makes
restore-into-the-same-database an ordinary lookup rather than a second mode. An
unresolved reference is a named refusal rather than a `None`, and what restore
does with one differs per table: `watch_states` and `media_items` refuse the
row and count it, `search_queries.clicked_title_id` is set `NULL` and counted.
`curated_rows` is never carried — it is the one precious-looking table where a
wrong id fails nothing at all.

**The command is `usher backup` rather than a documented `pg_dump`**, because
the runtime image carries neither `pg_dump` nor `psql` and adding
`postgresql-client` costs +62.1 MB on a 359 MB image. Disaster recovery is a
short restore plus a background rebuild instead of a crisis — the difference
between "lost everything" and "lost an afternoon of indexing".

**The artifact is gzip-compressed JSON Lines: a header object, then one object
per row carrying `table` and `row`.** Four properties earn that, and the first
is the one no Postgres-native format has:

- **Every reference is rewritten on the way out**, which `pg_dump -t
  watch_states` cannot express because there is nowhere in a custom-format
  archive to put a natural key.
- The *format* streams. ⚠️ **The shipped writer does not**, and holds every
  carried table in memory before writing a byte, which is affordable because
  the manifest keeps the carried set small and for no other reason.
- An operator can read it, which matters because this file is the only copy of
  the money ledger and of a household's history.
- It survives a Postgres version change, where `pg_dump -Fc` does not restore
  into an older server.

**The destination is written through a scratch sibling and `os.replace`d into
place**, so a run that fails part-way leaves the previous artifact intact
rather than replacing it with a truncated one — and a truncated gzip
decompresses cleanly up to the point it stops, so the failure it prevents is
silent. The guarantee is against a failed run and not against a power cut.

**The header carries two stamps and only one is enforced by refusal.**
`schema_revision` is Alembic's head as the *database* reports it, read through
the same function `/health/ready` uses, so "the app refuses to serve on a
schema mismatch" and "restore refuses rather than half-applying" are one
definition. `generated_at`, `manifest_version`, `usher_version` and the
per-table row counts are provenance and a self-check: the counts are `len()` of
what was written rather than a `count(*)` taken beside it, which is what lets a
restore read a short table as a truncated file rather than as a race.

⚠️ **`source_credentials` is carried as ciphertext and `usher backup` does not
decrypt it**, so an artifact restored into a deployment holding a different
`USHER_SECRET_KEY` restores credentials nobody can read. The command says so on
every run rather than behind a flag. **Keep `USHER_SECRET_KEY` with the
artifact.**

#### Restore — one transaction, four refusals

`usher restore <artifact> [--dry-run]`.

⚠️ **Its normal path is not an empty database.** `watch_states.title_id` is
`ON DELETE RESTRICT`, so into an empty catalog the load-bearing table's every
row fails its foreign key. What ships is **a short restore plus a background
rebuild**: the importers rebuild the catalog (`usher bootstrap --phase all`,
then `usher sync`, then `usher work`), and restore lands the precious rows on
top of it.

The operator-facing sequence is
[`docs/runbooks/restore.md`](../runbooks/restore.md) and its clock is
[`docs/runbooks/disaster-recovery.md`](../runbooks/disaster-recovery.md). **All
four runbooks this section asks for — restore, upgrade, disaster recovery and
rotation — are indexed at
[`docs/runbooks/README.md`](../runbooks/README.md).**

**Four refusals, in order, all before any write, and only the fourth is a
report.** (1) an artifact that is not readable or is truncated — including a
gzip member that ends early, which raises a bare `EOFError` and is therefore
*not* an `OSError` the CLI boundary would have caught; (2) a **schema
mismatch**, comparing the header's stamp against the *database's* revision and
naming both values; (3) a `table` key the manifest does not classify, which is
what an artifact from a later schema looks like; (4) **unresolved references,
collected across the whole file and reported together** — a restore that
stopped at the first missing title tells an operator to enrich one title, where
one that reports forty tells them the catalog is not finished.

**One transaction for the whole file**, so an unresolved reference in the last
row rolls back the first and "refuses rather than half-applies" is a property
of the code rather than a promise. ⚠️ **The consequence is that the failure
mode of a very large artifact is memory.** `--dry-run` resolves everything,
prints the identical report and commits nothing, which is how an operator
learns what would be refused without holding a transaction open.

**The report separates written, already present, nothing-to-write-onto,
skipped-as-unresolvable, and refused**, per table, with the refused list naming
the keys that were looked for — five numbers rather than one, because
"restored 9 rows" over an artifact holding 50 is the failure the command exists
to make visible. A run with any refusal exits non-zero.

⚠️ **"Already present" and "nothing to write onto" are two numbers because they
are opposite instructions.** `media_items`' merge is an `UPDATE` over a row the
source walk creates, so "the target already holds this link" and "there is no
row here at all" both write nothing and read identically — and the second is
the **normal** state of a first restore into a rebuilt deployment.

**The refused list is capped at 20 named rows with an exact tail.** The
per-table counts are computed from the whole list and stay exact whatever is
printed.

**The header's per-table row counts are a truncation gate**, compared against
the body before any write — because the artifact is readable and editable by
design, and every hand-edit that drops a line leaves the header saying how many
there should have been.

#### `--skip-unresolvable`, and why the default does not move

A correctly rebuilt catalog can refuse a whole artifact over links to stubs
carrying neither an `imdb_id` nor a `tmdb_id` — rolling back the household, the
source, its credential, every resolved watch state and every search query with
it. The rows that cost the restore are the rows whose loss costs nothing: a
link the match ladder would have re-derived is re-derived to the same answer.

**`usher restore --skip-unresolvable` drops those rows and commits the rest.
The default is unchanged and is not weakening.** "Refuses rather than
half-applies" is this command's headline guarantee, so the escape is an
operator explicitly accepting a loss rather than a heuristic, and the dropped
rows are counted in a bucket of their own. It composes with `--dry-run`.

⚠️ **It covers exactly the references the importers rebuild.** A household the
target does not hold, a source colliding on a name, and a credential whose
source is absent all still refuse with the flag set: none of them is "this
catalog is at a different bootstrap phase". Skipping a household would silently
drop every watch state in the file.

**"Insert" is the wrong rule for five of the eight carried tables:**

| table | rule |
|---|---|
| `users` | insert if the name is absent (`uq_users_name`); otherwise every reference adopts the id the target already holds |
| `sources` | insert if the id is absent; **refuse** if a *different* source holds that name |
| `source_credentials` | insert on `ref`, `DO NOTHING`; refuse if its source is not here |
| `watch_states` | upsert on `uq_watch_states_user_title` / `uq_watch_states_user_episode`, whichever the row's target names |
| `llm_calls` | insert on `id`, `DO NOTHING` — append-only, which is what makes restoring one spend ledger twice safe |
| `row_provider_settings` | upsert on `slug_prefix` |
| `search_queries` | insert on `id`, `DO NOTHING`, `clicked_title_id` nulled where unresolved |
| `media_items` | update the two links **only where the target's `title_id` is `NULL`** |

⚠️ **The `sources` refusal cannot lean on the database.** There is no unique
index on `sources.name`, so two sources pointing at one server is a state this
schema permits, an `ON CONFLICT (name)` would not compile, and the refusal is
an explicit read. `users` really does have `uq_users_name`, which is why its
rule is one clause and this one is three.

**Restoring the same artifact twice is a no-op on the second run** — every
table's count identical — because that is the operator's instinct after a
partial failure. It is what `DO NOTHING` on `llm_calls` buys, and what the
`IS DISTINCT FROM` guards on the two upserts buy.

**What "rebuildable" means, table by table, where it is not obvious:**

| Table | Rebuildable? | From what, at what cost |
|---|---|---|
| `people`, `credits`, `collections` | **yes, with no network call at all** | `raw_payloads`, via `usher derive --backfill`. The payload cache is the backup |
| `user_taste` | **yes** | a mean over embeddings of the household's watch states; it carries its own fingerprint, so a missing row is indistinguishable from a stale one and is recomputed by the same predicate rather than restored |
| `title_neighbors` | **yes** | `usher similar --rebuild`, and `blend_fingerprint` is what tells a restored table from a current one |
| `genome_scores`, `genome_tags` | **yes, but only from upstream** | re-download `ml-latest.zip` and re-run `bootstrap --phase movielens`. **Not guaranteed**: GroupLens can withdraw or replace the archive, and then it is not rebuildable at all. They share a `genome_revision` and are restored together or not at all |
| `curated_rows` | **yes, and cheaply — but not to the same rows** | one completion per household. It is the first table whose contents no re-run reproduces: there is no oracle and it is not deterministic, so "rebuildable" means *a screen appears*, not *the screen comes back* |
| `raw_payloads` | **yes, at the price of the whole crawl** | the closest call in the classification. Against carrying it: it is the third-largest relation in this database and is third-party payloads verbatim, so rule 1 of [04](04-catalog-bootstrap.md) bites. For carrying it: refilling it is hours of requests against a server this project does not own. **`usher backup --include-payloads` is deliberately not built** — a flag that makes the artifact redistribute TMDb payloads is a licensing decision rather than an operator convenience |
| **`llm_calls`** | **NO. From nothing.** | It is a **spend ledger**, and the only record that money was spent. It cannot be recomputed from the catalog, from `curated_rows` (replaced nightly), or from the provider — no OpenAI-compatible endpoint offers a per-key call history, and the price applied is a setting at the time of the call that a later price change would silently rewrite |

**`search_queries` is precious too.** It is the record of what a household
typed and what it then played, nothing re-derives it, and the plan to evaluate
search quality depends on it surviving.

**The general form.** A prose table is updated by whoever remembers; the two
tables nobody remembered were the two that were wrong. That is the argument for
the manifest, and it is why the enforcement is a test rather than a convention.

### Resource envelope

**These are sizing estimates for an operator provisioning a disk.** Nothing
reads them and no host enforces them. 🔴 A milestone once treated the Postgres
row as a *budget*, derived a **2.0 GB** ceiling from it, measured a design at
**2.702 GB** and **withdrew the design** — against a number with no forcing
function behind it. A figure here is a thing to buy a disk against, never a
thing to refuse a design against. Measured on a real 1,272,367-title catalog
with 130,647 titles enriched: `pg_database_size` **5,025,650,355 B (4,793 MB)**.

| | |
|---|---|
| Postgres, catalog + indexes | **~5 GB at 1.27M titles with 10% enriched**, and the enriched fraction is what moves it: `raw_payloads` is 995 MB of that, `title_embeddings` 298 MB, `title_neighbors` 572 MB. A fully-enriched catalog is several times larger |
| Postgres, + the IMDb people/credits load | **+3.4 GB** — 12,637,249 credits over 3,215,476 people, measured after `VACUUM FULL`. Not loaded by default |
| Postgres, + `titles.credit_names` | **+624 MB settled, +1,368 MB transient** before a vacuum — the peak is what an operator's disk sees |
| A migration that rewrites a large column | Budget the **peak**: `UPDATE credits SET source = 'tmdb'` over 2,877,486 rows left a dead tuple per live one (**794 MB → 1,431 MB**, +637 MB transient, 50 s), and `VACUUM FULL` afterwards settled it at **740 MB**, 54 MB *below* baseline — but a migration runs no `VACUUM` |
| HNSW (`halfvec`) | 🔶 At 384 lanes, **146 MB** of index inside a **278 MB** `title_embeddings` relation over 130,673 embeddings; rebuilt at 1024 lanes (**2,048 bytes** a vector against **768 bytes**) it is **340 MB** inside **707 MB** — 2.33× and 2.54×, both under the 2.67× the lane count alone predicts. The earlier **~1.5 GB** projection at full catalog coverage extrapolates to **~3.5 GB**, and is labelled as an extrapolation |
| Image cache | **Bounded per image and unbounded over time.** The ladder bounds the cache at four entries an image by construction; **nothing evicts**, so the size is `images browsed × up to four rungs × their bytes` and **the growth driver is browse coverage, not catalog size**. ⚠️ There is no LRU ceiling to configure and no eviction method to call: the four real settings are `image_cache_dir` (where), `image_max_bytes` (a **per-image** 5 MiB refusal, never a cache cap), `image_fetch_timeout_seconds` and `image_cdn_base_url`. Measured at 0.06% browse coverage, extrapolating to one image per title gives **~225 GB**, and all four rungs **~668 GB**. This directory is half of [10](10-telemetry-and-dashboards.md)'s *Disk projection* alert |
| Postgres, largest relations | `title_neighbors` **1140 MB**, `titles` **1050 MB**, `raw_payloads` **995 MB** at the reading above |
| Usher process | **~500 MB–1 GB**, plus **~200 MB** for the embedding model — the model half applies to the `fastembed:` runtime only. Under `openai:` no model is loaded in this process at all |
| Embedding model | 🔶 **~130 MB** on disk was the previous default checkpoint's measured download; the shipped default is declared at **1.2 GB** — the price of the 1024-wide column — and is **0 on the `openai:` runtime** |

**A full `usher similar --rebuild` is an overnight job, not a follow-on step.**
`nearest_for` runs under `enable_indexscan = off` by design, so it is an exact
scan whose working set exceeds both `shared_buffers` and this host's L3. Plan a
recovery against the figure in *Scheduled work* above.

Tuning that matters: `maintenance_work_mem` high enough to avoid the
`hnsw graph no longer fits into maintenance_work_mem` notice during index
builds, `max_parallel_maintenance_workers = 7`, and GIN `fastupdate = off`.
