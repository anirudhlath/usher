# 08 — Operations

## Configuration

Three layers, split by what changes and when:

| Layer | Holds | Changes |
|---|---|---|
| **Environment** | `DATABASE_URL`, port, log level, embedding model, `USHER_SECRET_KEY`, TMDb key, ✅ the LLM endpoint, model and key (M8) | Deploy time |
| **Config file** (TOML) | Rate limits, TTLs, enrichment tier. 🔴 **Not the image cache ladder** — it said so until 2026-08-11 and the ladder is a code constant ([ADR-0032](decisions/0032-the-image-proxy-clamps-to-a-ladder.md)): mechanism before setting, and a knob nothing reads is dead config wearing a control's name | Restart |
| **Database** | Sources, users, ✅ row provider enable/disable (**M9** — see below) | Runtime, via admin API |

Sources live in the database because they are added through the admin API. A
deployment that needs a compose edit and a restart to connect a media server is
the wrong shape for this.

Until the TOML layer exists, everything in the first two rows is an
environment setting on `usher.config.Settings` and is documented in
`.env.example` — completeness in both directions, so a setting an operator
cannot discover and a documented key that is not a setting are both test
failures (`tests/unit/test_deployment_config.py`). M6 added nine of them,
four `USHER_EMBEDDING_*` and five `USHER_SEARCH_*` — **seven `USHER_EMBEDDING_*`
since `m09e` added `_BASE_URL`, `_API_KEY` and `_TIMEOUT_SECONDS`**, all three
read only by the `openai:` embedding runtime and none of them by the
`fastembed:` one, which is the shape `USHER_QUERY_EXPANSION_ENABLED` below has:
a setting whose relevance is decided by another setting's value. ⚠️
**`USHER_EMBEDDING_BASE_URL` is deliberately not `USHER_LLM_BASE_URL`** — they
are one endpoint on many hosted providers and two processes here, because vLLM
serves one model per process, so collapsing them would make *"point the embedder
somewhere else"* impossible without moving curation too
([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)).
**M8 added eight
`USHER_LLM_*` plus `USHER_CURATION_POOL_SIZE` and
`USHER_QUERY_EXPANSION_ENABLED`** — ten. **M9 adds four `USHER_IMAGE_*`** —
`_CACHE_DIR`, `_MAX_BYTES`, `_FETCH_TIMEOUT_SECONDS` and `_CDN_BASE_URL` —
and the interesting one is the fifth it does **not** add: the width ladder is a
code constant, for the reason the middle row above now carries
([ADR-0032](decisions/0032-the-image-proxy-clamps-to-a-ladder.md)).
`USHER_IMAGE_CDN_BASE_URL` is a setting for `USHER_TMDB_BASE_URL`'s reason (a
household behind a restrictive network puts a proxy in front) and is *also* the
answer to a question that would otherwise be a network call: resolving the
provider's `secure_base_url` per cold image is a second round trip, against an
authenticated endpoint, for a value that changes approximately never. And
`USHER_IMAGE_CACHE_DIR` is the **fifth** entry in `compose.yml`'s
`environment:` block, which had held four since M5 — a bind-mount path is a
topology fact in exactly the way a database hostname is, and the other three
are the operator's. That last one arrived on 2026-08-07
and is the one place this project ships **two** switches over one dependency:
`USHER_LLM_ENABLED` builds the client, and query expansion is off even when it
is on, because the retrieval measurement in
[05](05-search-and-similarity.md) put expansion's effect the wrong way round
(MRR 0.733 → 0.373). Setting it true with no client is refused at startup
rather than ignored, which is this document's dead-config rule applied to a
*state* rather than to a typo. `USHER_CURATION_POOL_SIZE` is worth
its own line because it looks like the "row weights" case below and is not:
the pool is assembled, sent and discarded, so there is no half-computed
artefact, and what the number is really about is the *context window of
whatever model `USHER_LLM_BASE_URL` names* — a deployment fact, measured at
**~20.4 prompt tokens a candidate**
([ADR-0028](decisions/0028-the-pool-is-the-contract.md)), which an operator
must be able to change without editing code. **Its sibling is
`USHER_LLM_MAX_OUTPUT_TOKENS`, not the row scores — and 🔶 nothing couples
them, which is a live gap.** The endpoint's constraint is
`prompt_tokens + llm_max_output_tokens ≤ max_model_len`, so raising the output
ceiling silently lowers the workable pool, and the failure arrives as a parked
job rather than as a startup refusal. That is the one place this document's own
`_query_expansion_needs_a_client` shape — refuse an impossible *state* at
startup — is **not** applied, because `max_model_len` is a property of the
endpoint that no setting in this file knows. ⚠️ `le=1000` is a ceiling the
reference endpoint cannot serve: measured 2026-08-07, pool 1,000 and 700 both
return HTTP 400 and 600 works at 12,540 prompt tokens. **M7 added none**, and
that is recorded so the count reads as a current statement rather than as a
tally somebody stopped keeping: every M7 constant that could have been a
setting is deliberately code — the similarity weights and pool sizes
([05](05-search-and-similarity.md)), the row scores and the composition caps
([06](06-rows-and-recommendations.md)), and the row build's concurrency, which
has no setting because it has no mechanism to bound (below).

**Two entries that were in that middle row will not become settings, and M6 is
where that was decided rather than drifted into. They are struck from the table
above in M7, which is the milestone that made leaving them there concretely
wrong** — a table listing a knob after its own prose retracted it is the same
failure as a table listing a control nothing implements.

- ✅ **"Concurrency per lane" now has a knob, because M9's W1 built the
  mechanism it would bound.** It was struck on the principle that *a setting
  cannot be added ahead of the mechanism it would bound* — there was no
  semaphore anywhere in `src/` — and the principle is unchanged: the setting
  arrives **with** the pool rather than before it. `USHER_JOB_CONCURRENCY` is
  the worker's global ceiling; the per-kind ceilings under it are code
  (`usher.services.jobs.KIND_CONCURRENCY`) for the reason the row weights below
  are, one entry per `JobKind` with its measurement beside it. The *row build*
  still has no such setting, and the bullet at the end of this section says
  why: its mechanism is still a `for`.
- **"Row weights" are deliberately module constants, not configuration.**
  M6's similarity blend and its ranking blend are both weighted sums, and
  both are hardcoded. Changing a weight changes what "similar" and "relevant"
  *mean*, and every row already written to `title_neighbors` was written
  under the old meaning — so an operator turning a dial silently gets a table
  half computed one way and half the other, which is this milestone's own
  headline failure mode in a config file. A weight change is a code change
  and a rebuild.

  **M7's row provider scores are the same answer for different reasons**, and
  they are worth stating because the M6 argument does not transfer: a row
  score is computed per request and cached for ~30 s, so there is no
  half-written artefact to be inconsistent. Two other reasons hold instead.
  A configurable score set can *reorder* Continue Watching, which
  [06](06-rows-and-recommendations.md) fixes as *"1 row, always ranked
  first"* — a TOML file that can break a specification. And a score only
  decides ordering *among proposals*, after which diversity constraints and
  the top-N cap reshape the result, so an operator turning the dial would
  watch a screen change for reasons the dial does not explain.

✅ **"Row provider enable/disable" was annotated rather than struck, because
unlike the two above it is a control that should exist — and in M9 it does.**
The bottom row claims it is available *"runtime, via admin API"*, and the admin
API was M9's; M6 added no route and M7 added exactly one, `GET /home`. Until
then the mechanism was missing on the same principle the concurrency bullet
states:

> A `row_providers` table with ten rows all reading `enabled = true` is
> indistinguishable from no table, right up until an operator finds it and
> expects toggling it to do something. **Providers are enabled by registration
> in code** — `services/rows/__init__.py`'s `ROW_PROVIDERS` is the
> composition point, nine entries in M7 and **ten since M8 registered
> `CuratedProvider`** — and the runtime control lands with the admin API that can
> write it. **M9**, and [09](09-roadmap.md)'s M7 boundary call 9.

**M9 discharged it, and the refusal's own condition is what the discharge had
to satisfy.** `row_provider_settings(slug_prefix PK, enabled, updated_at)`
(migration `m09a`) is written by `PUT /admin/rows/providers/{slug}` and read by
every composer ([07](07-client-api.md),
[06](06-rows-and-recommendations.md)) — so toggling it does something, which is
the sentence above turned into a requirement. The half of the refusal that
survives is that the table is created **empty and is never seeded**: absence
means enabled, exactly as *"enabled by registration in code"* already meant, so
there is no state where the table exists and says nothing, and no migration
carrying a second copy of the registry.

This is the same argument [10](10-telemetry-and-dashboards.md) makes about
`search_queries`: a table whose writer does not exist gets its shape fixed
before anything has tried to fill it.

**And there is no concurrency setting for the row build**, for the reason the
first bullet gives: a setting cannot be added ahead of the mechanism it would
bound, and boundary call 8's mechanism is a `for` loop whose correct value is
1 ([01](01-architecture.md)'s concurrency table now carries the row). Stated so
the absence reads as a decision.

### `.env` has two readers, and that is what the `USHER_COMPOSE_` namespace is for

Docker Compose reads `.env` to substitute `${...}` into `compose.yml`;
pydantic-settings reads the same file as a settings source with
`extra="forbid"`. The two vocabularies overlap, so a variable meaningful only
to compose is an *extra* input to `Settings` — and one such key
(`USHER_HOST_PORT`, the host-side publish port) made `cp .env.example .env`,
the documented first step, fail every entry point with
`ValidationError: usher_host_port` from M1 until it was found by M5's smoke
test.

`extra="forbid"` stays, because it is what turns `USHER_LOG_LEVL=DEBUG` into
a startup failure instead of a line in `.env` that silently does nothing. The
two readings are separated by **name** instead: `USHER_COMPOSE_*` belongs to
`compose.yml` and the application drops it before validation; every other
`USHER_*` key is a setting or a typo. That is a rule the next compose variable
can satisfy rather than a list somebody has to remember to extend.

### A documented setting has to reach the container

`compose.yml` gives the `usher` service the whole `.env` (`env_file:`), not a
hand-maintained `environment:` list. The two are different mechanisms:
`environment:` names one variable at a time and compose substitutes its value;
`env_file:` hands the file over. The list form forwarded 5 of 30 documented
keys, so 24 — `USHER_WORKER_ENABLED` among them — were documented, worked when
delivered directly, and were silently ignored when set where the docs point.
**A setting that is documented but unreachable is dead config that looks like
a control**, and this one had teeth: an operator who sets
`USHER_WORKER_ENABLED=false` and then runs `usher work` in a second container
gets two workers, and `JobWorker.startup()` requeued everything `running`, so
each stole the other's live claims. *(That consequence is closed by M9's W1 —
recovery is a lease now — but the finding about `env_file:` is unchanged, and
two workers still spend `USHER_JOB_CONCURRENCY` and
`USHER_TMDB_REQUESTS_PER_SECOND` twice against limits that are per process.)*

`environment:` still wins over `env_file:`, so what is left in it is exactly
the four the compose *topology* owns rather than the operator:
`USHER_DATABASE_URL` (the service's hostname on the compose network),
`USHER_HOST`/`USHER_PORT` (what `ports:`, the Dockerfile's `EXPOSE` and the
healthcheck all assume) and `USHER_SECRET_KEY` (substituted as `${...:?}` so a
missing key fails at `docker compose up` with a sentence rather than as a
container that starts and dies on validation).

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
- **And neither does a rejected *setting*.** The same defect, one surface
  over, found while building the CLI's error boundary and fixed with it:
  `Settings` rejecting `USHER_DATABASE_URL` printed
  `input_value='mysql://admin:<the password>@db:5432/usher'` in the
  traceback, and a truncated `USHER_SECRET_KEY` printed the key. Both fields
  are `SecretStr` precisely so that cannot happen; the CLI was the one reader
  that unwrapped them, on the surface an operator is most likely to paste
  into an issue. `usher.cli._settings_problem` renders `loc` and `msg` and
  drops `input`, the same trade `usher.api.errors` makes — the operator still
  learns which setting was wrong and what it should have been, and never sees
  the value. **`--traceback` does not reopen it**: a settings failure's stack
  is six pydantic frames that diagnose nothing, so the only thing re-raising
  would add is the credential
  ([ADR-0026](decisions/0026-the-cli-boundary-names-families.md)).
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
- At the config layer, `database_url`, `secret_key`, `tmdb_api_key` and — ✅
  since M8 — `llm_api_key` are held as `pydantic.SecretStr` and unwrapped only
  at the point of use, so the rules above are enforced by the type system, not
  just convention. **`llm_api_key` is the first credential this project hands
  to a third party it did not choose**: `USHER_LLM_BASE_URL` is a setting, so
  the upstream is whatever an operator points it at. It travels in an
  `Authorization: Bearer` header and never in a URL — `HTTPXClientInstrumentor`
  records the full URL as a span attribute, which is the same reason
  `TmdbClient` prefers a bearer token — and no exception message in
  `adapters/llm/` carries a URL or a request body, because the request body
  here *is* the prompt and the prompt carries the household's watch history.
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
| Push socket drops | Backoff reconnect; delta reconcile on reconnect; after N failures mark `supports_push = false` and lean on the nightly walk. **The failure counter resets on delivery, not on connection** — a proxy that upgrades and then buffers connects perfectly every time, so a counter reset by connecting never reaches the ceiling and this row silently never fires ([ADR-0018](decisions/0018-push-health-is-a-message-ledger.md)). |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected. |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades. |
| Provider image CDN unreachable | ✅ M9: catalog and every rendered card unaffected — an artwork reference is a row, not a fetch, so browsing, search and the home screen never touch the CDN. A **cold** image (no entry for that `(image id, rung)`) answers `503 source_unavailable` with `Retry-After`; a **cached** one still serves, because `GET /images/{id}` reads the disk before the network. The CDN needs no credential, so this row has no authentication arm. An answer the proxy cannot serve splits in two: artwork this deployment declines to carry (an `image/svg+xml` logo, ~1 title in 17) is an ordinary `404 not_found`, and everything else — a 4xx, a body past `image_max_bytes`, a captive portal's HTML under a 200 — is `503 source_unavailable` with **no** `Retry-After`, since re-asking produces the same answer. That second one's honest status is a 502 and [ADR-0030](decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)'s closed vocabulary has no code for one. |
| LLM call fails | Previous curated rows persist. Home composes without them. ✅ M8: the failure is fatal to the *job* and never to the screen — a failed generation never reaches `replace_for_user`, so this row is a property of the control flow rather than of a transaction. **Only the failures that translate to `PortDataMalformed` park the job** rather than retrying into the same answer: a 4xx that is none of 429, 401/403 or 408 (so 400, 402, 404, 409, 422), a 200 whose body does not conform, and a generation that validated to zero rows. The other three families **back off** — `JobWorker` parks on `PortDataMalformed` alone and marks every other `UsherPortError` retryable (`services/jobs.py`, `JobWorker._run`), so 429 (`PortRateLimited`), 401/403 (`PortAuthFailed`) and 408 or any 5xx (`PortUnavailable`) all retry with jittered backoff. *(This sentence read "a 4xx that is not 429 parks the job" until 2026-08-07, which over-parked three families; measured against the adapter's `_decode` and `JobWorker`'s two `except` arms.)* |
| LLM call fails during a **search** (query expansion) | ✅ M8: the search runs on the query the user typed and `expanded_query` is absent. **The attempt is still billed** — one `llm_calls` row per attempted call — so the warning arrives after the money. Off by default ([05](05-search-and-similarity.md)), which is why this is a narrower row than the one above. |
| Embedder unavailable | Semantic search falls back to full-text, flagged in the response. |
| Meilisearch down (if enabled) | Fall back to the Postgres index. It is never the only index. |
| Worker in its own process (`USHER_WORKER_ENABLED=false` on the server, `usher work` beside it) | ✅ M9: every SSE frame a *job* raises reaches a `NullEventPublisher` and no client is told — `title.updated` since M5, and `bootstrap.progress` since M9's E7 put `JobKind.BOOTSTRAP` on the queue. The bus is in-process and the `LISTEN/NOTIFY` implementation `ports/events.py` names has no owner. **Nothing durable is lost**: the catalog, `import_runs` and `sync_runs` are written by the worker either way, so `GET /admin/bootstrap/status` and `GET /admin/sources/{id}/status` report the same thing in both topologies and a client that heard nothing can still see where a run got to. The cost is latency to a *screen*, not correctness. |
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
- **A server-supplied `Retry-After` is a floor added to that same jittered
  delay, never a replacement for it.** ✅ M9: `PortRateLimited.retry_after`
  had been assigned at six sites across four adapters since M4 and read
  nowhere — a 429 that told this project exactly when to come back was
  answered with the queue's own jittered guess instead. `JobQueue.fail`
  now takes `retry_after_seconds`, and `JobWorker._fail` reads it off a
  caught `PortRateLimited` by `isinstance`, never by `getattr` (a future
  exception member must not accidentally opt into the behaviour). The hint
  is clamped at zero before it is added: a `Retry-After` carrying RFC 9110's
  HTTP-date form can already be in the past, and an unclamped hint would
  pull a rate-limited job's retry *earlier* than the ordinary schedule — the
  exact hot loop the backoff exists to prevent. **No ceiling is imposed on
  the hint itself** — a hostile or buggy upstream can ask for an arbitrarily
  long wait, bounded only by the attempt ceiling below and visible as
  `usher.jobs.queued` failing to drain. Recorded, not solved.
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
  reserved for work a human has to look at. The handlers
  (`usher.services.handlers`) are where this is decided — including `sync`'s
  own three ways of finding nothing to do (M9's E3): a source deleted between
  enqueue and claim; a source *disabled* between enqueue and claim, re-checked
  in the handler rather than trusted from the route's own 409, because
  head-of-line blocking (below) can hold the row behind another walk for
  minutes — long enough for an operator to park a source that was healthy when
  they pressed the button; and a source whose credential row has gone missing,
  which `composition.open_adapter` already answers `None` for. A job whose
  *key* is unparseable is the opposite case and does park, because that is a
  real defect somebody has to see.
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
- **Abandoned claims are recovered on a lease, and the lease is what makes
  recovery possible at more than one worker.** This read *"startup requeues
  anything left `in_progress` by an unclean shutdown"* until M9's W1, and that
  is what shipped: `requeue_running()` with the port's `older_than_seconds=0.0`
  default, called once at process start. M9's S3 measured the dead end. One of
  three workers died holding 20 claims; the only lever that could recover them
  would have requeued the other two workers' **live** claims with it, so the 20
  were written off and reported as part of the shortfall. Two changes, and
  neither works without the other:
  - `JobWorker.recover()` passes an explicit `USHER_JOB_LEASE_SECONDS`
    (default 300), so it takes back only claims nobody has touched for a
    lease — and is therefore safe to run **repeatedly**, which is what lets a
    live worker recover a *dead peer's* orphans rather than only its own.
  - `JobQueue.touch()` is the heartbeat. Without it the lease would have to
    exceed the longest job a deployment can run — a `bootstrap` phase is
    measured in hours — and the orphan window would be hours with it. The
    worker beats every third of a lease for everything in flight, so the lease
    is a bound on *"the process stopped"* rather than on how long a job may
    take.
- **Head-of-line blocking is accepted, priced, and recorded — M9's E3, and the
  one lane this queue has.** `POST /admin/sources/{id}/sync` (`JobKind.SYNC`)
  and `POST /admin/bootstrap/{phase}` put the two longest units of work in
  this system on the same single `JobWorker` lane every other kind shares —
  `services/jobs.py`'s claim loop is strictly sequential — so `enrich`,
  `index`, `derive`, `curate` and `match` are unavailable for the duration of
  either, hours in the sync case, triggered by an unauthenticated route. The
  queue is chosen anyway, for its dedup on `(kind, key)`, its durability
  across a restart (`JobWorker.startup()` requeues everything `running`), and
  the precedent `POST /admin/rows/regenerate` already ratified. It is bounded
  rather than unbounded — both handlers commit per batch, so no transaction
  spans the job — and `usher sync` / `usher bootstrap` remain the way to run
  one off the queue, at the cost of a second process rather than a second
  lane. No second lane is added to change this trade; a deployment large
  enough to need one is a deployment large enough to need `usher work` run
  from a second host instead.

  ✅ **M9's W1 narrows this without removing it, and the correction is worth
  reading precisely.** *"`services/jobs.py`'s claim loop is strictly
  sequential"* is no longer true — jobs run in a bounded pool — so `enrich`,
  `index`, `derive`, `curate` and `match` are **not** unavailable for the
  duration of a sync any more; they run beside it, up to
  `USHER_JOB_CONCURRENCY`. What survives is the *claim* ordering, which is
  where the real head-of-line blocking always was: the claim is `priority DESC,
  created_at`, so a bulk enqueue at one priority still defers everything
  enqueued after it — S3 measured `title_embeddings` frozen at 542 for a whole
  130,806-title crawl and then jumping to 4,929 within minutes of the enrich
  queue emptying, with the embedder on the entire time. A pool does not fix an
  ordering. *(The parenthetical above also read "`JobWorker.startup()` requeues
  everything `running`"; that is now `recover()` on a lease — see the recovery
  bullet.)*

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

Illustrative and abbreviated, not literal — the real, verified `compose.yml`
(M1 Task 13) also gives `usher` its own healthcheck and has more to say
about the `-h 127.0.0.1` above: without it, `pg_isready` defaults to a Unix
socket, which reaches `pgvector/pgvector:pg17`'s own *temporary* bootstrap
server on a fresh volume and reports ready roughly a second before the
real server is (verified directly, including against a false-positive
window reproduced twice). `./data/models` is not yet mounted by the actual
M1 `compose.yml` — nothing before M6 (embeddings) writes there.

The `env_file`/`environment` split above **is** literal, and it is the one
part of this snippet that should be read as normative: see "A documented
setting has to reach the container" at the top of this document. An earlier
version of this snippet showed `environment: [DATABASE_URL, USHER_SECRET_KEY,
TMDB_API_KEY]`, which is the shape that left 24 documented settings
unreachable.

- Alembic migrations run on startup; the app refuses to serve on a schema
  mismatch rather than guessing.
- First run detects an empty catalog and offers bootstrap through the admin API
  — it does not start a multi-hour download unprompted.
- Bootstrap is resumable and checkpointed; a restart mid-import continues.
- **The operator trigger is `usher` (also `python -m usher`), and it exists
  before the HTTP surface does.** `serve` (M1), `bootstrap` /
  `bootstrap-status` (M2), `sync` / `sync-status` / `unmatched` / `work`
  (M4), `push` (M5), `index` / `search` / `suggest` / `similar` (M6),
  `derive` / `home` (M7) and `curate` (M8) — all fifteen the parser
  advertises — are the CLI composition root, documented command by command in
  `README.md`; [07](07-client-api.md)'s `POST /admin/sources/{id}/sync` and
  the two `/admin/unmatched` routes are M9's and are built over the same
  services. **The list above was four milestones stale**, naming six of the
  fifteen, and is restated here in full rather than extended by one.
  Every one of them has to work against an *empty* database — a command an
  operator can only run after a successful sync is no use for diagnosing
  why the sync did not happen. `curate` is where that rule costs something,
  because an empty catalog is an empty candidate pool and there is no
  generation to run: it says so and exits 1 rather than buying a completion
  with a guaranteed empty answer, and it is the one path in
  [06](06-rows-and-recommendations.md)'s curation that writes no `llm_calls`
  row at all.
- **A command whose only job needs a subsystem this deployment does not have
  says so and exits 1.** `usher curate` with `USHER_LLM_ENABLED=false` has no
  `LLMClient` and therefore no `CurationService` to build — the composition
  root, not the service, is what knows that. Unlike `GET /home` (nine of ten
  row providers need no model, so the screen is shorter) and `usher work`
  (five of six job kinds need none, so `curate` is simply left unclaimed),
  there is nothing here to narrow to, and a run that printed an empty report
  and exited 0 would tell a cron entry that curation is running.
- **`--allow-full-retraction` is the only way past ADR-0015's ceiling**, and
  it is a flag rather than a configuration default because it is the one
  input that can mark a whole library unavailable.
- **A failure the operator can fix is a message; a failure they cannot is a
  stack.** M7's smoke test found `bootstrap-status` and `sync-status`
  answering an unreachable database with sixty lines of asyncpg and greenlet
  frames whose only operator-facing content was the last one. `main` has a
  single `try` around the whole dispatch which names the families an operator
  can act on — `OSError`, `SQLAlchemyError`, `httpx.HTTPError`,
  `ValidationError`, and since M8 the port taxonomy's transport half
  (`PortUnavailable`, `PortAuthFailed`, `PortRateLimited`) — and answers each
  with one line and exit 1; `usher --traceback <command>` re-raises.
  **`Exception` is deliberately not among them**, so a bug still gets its full
  traceback, and Ctrl-C exits 130 rather than printing one. Neither is
  `UsherPortError` itself: an adapter translates its transport's failures
  before they cross, so `httpx.HTTPError` is unreachable behind a port and the
  three transport members had to be named — but `RepositoryConflict`,
  `RepositoryNotFound` and `PortDataMalformed` keep their stacks, because
  several of their raise sites are deliberate tripwires for bugs in Usher's
  own code. Why those families and not `Exception`, why the settings case is
  redacted, and the per-family evidence for the M8 widening are
  [ADR-0026](decisions/0026-the-cli-boundary-names-families.md).

### Backup — the asymmetry is the point

| Rebuildable from importers | Precious |
|---|---|
| Catalog, embeddings, search index, neighbour tables, cached images, curated rows | **Watch state**, users, source config, manual unmatched resolutions, ✅ **`llm_calls`** (M8 — see below; it is rebuildable from nothing) |

The precious set is a handful of small tables. A documented `pg_dump` of those
turns disaster recovery into a short restore plus a background rebuild, instead
of a crisis. State this loudly in the README — it is the difference between
"lost everything" and "lost an afternoon of indexing".

**M7 added five tables and four of them are rebuildable, which is worth the
detail because "everything is rebuildable" is the kind of claim that is true
right up to the table it is not true of.**

| Table | Rebuildable? | From what, at what cost |
|---|---|---|
| `people`, `credits`, `collections` | **yes, with no network call at all** | `raw_payloads`, via `usher derive --backfill` ([03](03-sources-and-sync.md)'s stage 5). This is M4's boundary call 2 paying off: **the payload cache is the backup** |
| `user_taste` | **yes** | a mean over embeddings of the household's watch states. It carries its own fingerprint (`model_name` + `source_watermark`), so a missing row is *indistinguishable from a stale one* and is recomputed by the same predicate rather than restored. ⚠️ **And as of M7 nothing in `src/` calls `TasteService.centroid`**, so the table is unwritten on a running deployment — see below |
| `title_neighbors` | **yes** | `usher similar --rebuild`, and `blend_fingerprint` is what tells a restored table from a current one |
| **`genome_scores`** | **yes, but only from upstream** | re-download `ml-latest.zip` and re-run `bootstrap --phase movielens`. Frozen for three years, so reproducible in practice — **and not guaranteed**: GroupLens can withdraw or replace the archive, and then it is not rebuildable at all |

So the honest backup statement is that **`raw_payloads` and `watch_states` are
the load-bearing rows**, and `genome_scores` is the one M7 table whose
recreation depends on a third party still serving a file. It is not in the
precious column either, because a dump of it is a redistribution of MovieLens
data — permitted by `ml-latest`'s licence ([04](04-catalog-bootstrap.md)) and
still not something this project's own rule 1 does.

🔴 **M8 added three tables and one of them is the first thing in this project
that is not rebuildable from anything, at any price.**

| Table | Rebuildable? | From what, at what cost |
|---|---|---|
| `curated_rows` | **yes, and cheaply — but not to the same rows** | `usher curate`, one completion per household. Already covered by the left column above. ⚠️ It is the **first table in this project whose contents no re-run reproduces**: `title_neighbors` can be diffed against a fresh computation and `search_document` has a case asserting the stored value equals a freshly computed one; a curated row has no oracle and is not even deterministic at `temperature > 0`. So "rebuildable" here means *a screen appears*, not *the screen comes back* |
| `genome_tags` | **yes, but only from upstream** | the same `ml-latest.zip` and the same `bootstrap --phase movielens` as `genome_scores`, with the same caveat and the same third party. The two are written by one phase and share a `genome_revision`, so they are restored together or not at all — a vocabulary from one release over vectors from another mislabels 1,128 lanes, which is why `GenomeRepository.vocabulary` refuses a mismatch rather than answering `None` |
| **`llm_calls`** | **NO. From nothing.** | It is a **spend ledger**, and the only record that money was spent. It cannot be recomputed from the catalog, from `curated_rows` (which is replaced nightly, so last month's generations have no surviving rows), or from the provider — no OpenAI-compatible endpoint offers a per-key call history this project could read, and the price applied is a *setting at the time of the call* that a later price change would silently rewrite. Losing it loses the answer to "what did this cost", permanently |

**So the precious column has a fourth member as of M8: `llm_calls`.** It is
small — one row per generation per household per night, plus one per expanded
search — and it is append-only, which makes it the cheapest thing in the
precious set to back up and the most complete loss if nobody does. The
left-hand column's *"curated rows"* entry stays where it is and is correct;
the ledger beside it is not the same kind of object and was previously in
neither column.

⚠️ **`user_taste` is the one M7 table with no writer on the request path, and
that is a property of M7 rather than of backup.** `RowContext.taste` was
specified and deleted — every provider turned out to be a predicate over a
repository rather than a retrieval, and on the request path the centroid is
`None` unconditionally anyway, because it needs an embedder the route
deliberately holds none of. So `TasteService.centroid` has no caller in `src/`
and the table stays empty on a default deployment; what `TasteService` *is*
called for is `genre_affinity`, which needs no embedder and no centroid. The
table, its fingerprint and its written refusal are all built and tested;
the consumer is M9's, with the ranking terms
[05](05-search-and-similarity.md) names. Recorded here rather than left for an
operator to discover from an empty table.

⚠️ **Row provider enable/disable belongs in the *precious* column the day it
exists.** It is operator-authored state in the database, like source config —
no importer restores a human's choice — and the row above is annotated M9 for
exactly the reason it is not listed here yet.

### Resource envelope

**What this table is for, stated because its absence caused a real mistake.**
These are **sizing estimates for an operator provisioning a disk**. Nothing in
this project reads them, no host enforces them, and no policy is derived from
them. 🔴 M9's Track 2 treated the Postgres row as a *budget*, derived a 2.0 GB
ceiling from it, measured a design at 2.702 GB and **withdrew the design** —
against a number with no forcing function behind it. See
[ADR-0036](decisions/0036-the-imdb-tmdb-provenance-rule.md). A figure here is
a thing to buy a disk against, never a thing to refuse a design against.

**And the Postgres row was stale.** `~8–12 GB` described a database this
project no longer has. Measured 2026-08-12 on a real 1,272,367-title catalog
with 130,647 titles enriched — `pg_database_size` **5,025,650,355 B (4,793
MB)**, at `m09c`, with 130,647 embeddings, 3,266,225 `title_neighbors`,
2,877,486 `credits` and 129,131 cached payloads:

| | |
|---|---|
| Postgres, catalog + indexes | **~5 GB at 1.27M titles with 10% enriched**, and the enriched fraction is what moves it: `raw_payloads` is 995 MB of that, `title_embeddings` 298 MB, `title_neighbors` 572 MB. A fully-enriched catalog is several times larger. |
| Postgres, + the IMDb people/credits load | **+3.4 GB** — 12,637,249 credits over 3,215,476 people, measured after `VACUUM FULL` ([ADR-0036](decisions/0036-the-imdb-tmdb-provenance-rule.md)). Not loaded by default. |
| **Running the `m09d` migration** | **+637 MB transient on `credits`, and 50 s**, at 2,877,486 credits: `UPDATE credits SET source = 'tmdb'` leaves a dead tuple per live one (794 MB → 1,431 MB), and `SET NOT NULL` then scans the table. `VACUUM FULL` settles it at **740 MB, 54 MB *below* baseline** — but the migration does not run one, so **budget the peak**. Both scale with the enriched tier. |
| Postgres, + `titles.credit_names` | **+624 MB settled, +1,368 MB transient** before a vacuum — the peak is what an operator's disk sees |
| **Running the `m09e` migration** | 🔶 The only entry here that *frees* space, and the only one whose settled figure is unknown. It deletes every row of `title_embeddings` (130,673, in a 278 MB relation), `user_taste` and `title_neighbors` (3,266,175) and rebuilds the HNSW index empty. It runs no `VACUUM`, so the dead tuples are still on disk when it returns, and **the catalog then re-grows to an unmeasured size** at 1024 lanes as the backfill and `usher similar --rebuild` run. Budget for the old figures until the re-embed reports ([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)). |
| HNSW (`halfvec`) | 🔶 **~1.5 GB at full embedding coverage was projected at `halfvec(384)` and is now a floor.** Measured immediately before `m09e` on 2026-08-13: **146 MB** of `ix_title_embeddings_hnsw` over **130,673** embeddings, inside a **278 MB** `title_embeddings` total relation. `m09e` doubled the lane count to 1024 — 2,048 bytes a vector against 768 — and the rebuilt index's size is **owed rather than extrapolated**: the re-embed is in flight, and a per-row graph cost is not a linear function of the vector's bytes. |
| Image cache | Grows with use; capped by a configurable LRU ceiling |
| Usher process | ~500 MB–1 GB, plus ~200 MB for the embedding model — **the model half applies to the `fastembed:` runtime only**, and was measured for `bge-small-en-v1.5`. Under `openai:` no model is loaded in this process at all; the memory is the inference server's. |
| Embedding model | 🔶 **~130 MB on disk was `bge-small-en-v1.5`'s measured download.** The shipped default is now `fastembed:BAAI/bge-large-en-v1.5`, which fastembed 0.8.0 declares at **1.2 GB** against bge-small's 0.07 — the price of the 1024-wide column, and the reason this row is called out rather than quietly edited ([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)). Its download has not been re-measured, and it is **0 on the `openai:` runtime**. |

Tuning that matters: `maintenance_work_mem` high enough to avoid the
`hnsw graph no longer fits into maintenance_work_mem` notice during index
builds, `max_parallel_maintenance_workers = 7`, and GIN `fastupdate = off`.
