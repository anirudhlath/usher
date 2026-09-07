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

### Starting the app is not a command to walk your library

🔴 **It was one until 2026-08-19, and nothing said so** (issue #9). With
`USHER_PUSH_ENABLED=true` — the shipped default — the server starts a push lane
per **enabled** source, and the lane closes its reconnect gap with a delta
walk. A delta resumes from the newest **completed** item walk; a deployment
that has never completed one has no cursor, so the "delta" reads every item the
source has. On the household this project measures that is **1,126,789 items**
as counted on 2026-08-02 — re-measured at **1,134,919 items over 5,675 pages**
on 2026-08-15 by M10's S1, which also priced the walk at **~9.5 h** at its
6.04 s pooled mean per page. The library grows, so both are right on their
dates and the later one is the one to quote;
`.claude/rules/emby-push-and-ingest.md` carries the series. Either way it is a
walk performed by `uvicorn` with default settings, on a media server the
operator may not own, with no command issued. An earlier probe ran exactly that walk
before someone killed it, and M9's live run had to set
`USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false` to keep its request
budget statable.

**The decision: the lane closes gaps, and the first walk is a command.** A gap
is the window a socket was down; with no completed walk the window is the
entire catalog, and importing a catalog is `usher sync`'s job. So
`USHER_PUSH_GAP_CLOSE` is a closed vocabulary, defaulting to a refusal:

| Value | The lane does |
|---|---|
| **`cursored`** (default) | Closes a gap that has a cursor. With none, logs a `WARNING` naming the source and pointing at `usher sync`, and walks nothing |
| `always` | Walks when there is no cursor — and logs a `WARNING` naming the source and saying it is about to, *before* it starts. ⚠️ **Not quite the pre-2026-08-19 behaviour**: the walk still passes through `USHER_PUSH_GAP_MAX_ITEMS` (M10 S6), so at its default it stops after 20,000 items and records `FAILED`. `USHER_PUSH_GAP_MAX_ITEMS=0` alongside is what restores an unbounded gap close. The two settings compose rather than override: one decides whether a cursorless walk happens, the other bounds how large any gap close may get |
| `never` | No gap-closing walk at all, logged at `INFO` each time. **This has a cost**: Emby does not re-deliver what a disconnected client missed, so a change made during an outage waits for the next `usher sync` |

**What changes for an existing deployment:** only one that has never completed
an item walk for a source. Every source past its first `usher sync` (or its
first `POST /admin/sources/{id}/sync`, or one nightly cron run) has a cursor,
takes the same bounded delta it always took, and sees no change at all. A
brand-new source no longer populates itself as a side effect of the push lane
reconnecting — run `usher sync --source "<name>"` once, which is the step
[the README](../../README.md) already documents.

**Neither the rate limit nor a truncation was the answer, and both were
considered.** `USHER_PUSH_GAP_MIN_INTERVAL_SECONDS` bounds how *often* a gap is
closed and says nothing about how large the walk is — it was already at its
default of 60 s while the unbounded walk ran. And a cap on items would end a
run that then records `COMPLETED`, whose `started_at` becomes the floor for
every later delta: everything the truncated walk never reached would be skipped
silently and permanently. A walk of this kind is performed whole or refused
whole, so the setting is a decision rather than a limit.

**A commanded walk is never gated by this.** `usher sync`,
`POST /admin/sources/{id}/sync` and the nightly cron entry all reach
`ReconcileService` directly; the setting governs `LaneSupervisor._close_gap`
alone, which is the one caller no operator asked for.

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
  command handles the bulk case. ✅ **That command is built** — `usher
  rotate-secret --new-key-env <VAR>`, M10's K7, and its own section is below.
  ⚠️ **"On next write" is not a mechanism that
  keeps a deployment limping — there is no such path.** `PostgresCredentialStore.put`
  encrypts with whatever cipher it was built with, so the only "next write" that
  re-encrypts anything is *a credential an operator re-types*. Without the
  rotation command, rotating the key strands every stored credential. **Until that write happens the old rows are
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

### Rotation — `usher rotate-secret`, one table, per-row commit (K7)

✅ **`usher rotate-secret --new-key-env <VAR>` is built** —
`src/usher/services/rotation.py` owns the order of operations and
`db/repositories/credentials.py`'s `PostgresCredentialRotationStore` owns the
column.

```bash
export USHER_NEW_SECRET_KEY=$(openssl rand -hex 32)   # export, never .env
uv run usher rotate-secret --new-key-env USHER_NEW_SECRET_KEY
# then set USHER_SECRET_KEY to that value and restart
```

**What the key protects, surveyed rather than assumed.** There are exactly two
HKDF-SHA256 derivations over `USHER_SECRET_KEY`, differing only in their `info`
string: `build_cipher` (`usher.source-credentials.v1`) encrypts the JSON
`{username, password}` blob in `source_credentials.ciphertext`, and
`build_ticket_cipher` (`usher.playback-ticket.v1`) encrypts a playback target
URL inside a ticket that is **never persisted**. So rotation touches **one
table**, at one row per configured source — measured 2026-08-25 on this
deployment: `SELECT count(*) FROM source_credentials` is **1** and the relation
is **48 kB**. Saying that plainly is what keeps the command small.

⚠️ **The ticket cipher needs no rotation, and that is a fact about a ticket's
lifetime rather than an omission.** Rotating invalidates every outstanding one;
a client meets that as `404 ticket_invalid` and answers by asking `/play`
again. **"Short-lived" is five minutes** —
`api.routers.playback.TICKET_TTL_SECONDS`, re-read 2026-08-26 — and it is a
constant at the route rather than a setting, deliberately, because
`services/playback_ticket.py` states that *no TTL constant lives here*
(`redeem`'s `ttl_seconds` is required with no default) and
`USHER_PLAYBACK_TICKET_TTL_SECONDS` appears in this repository **only** as the
name that rule refused: `Settings` has no such field. The report prints the
consequence in one line and the command does nothing about it. ⚠️ **Keyset
cursors are outside this entirely**: `api/cursor.py` records that
`Settings.secret_key` is deliberately *not* what signs one.

**The new key is read from the environment and never from an argument.** A key
on a command line is in the shell's history file and in `ps` output for every
user on the box, and neither is undone by the command exiting, so
`--new-key-env` names the **variable** and the value is never a token
`argparse` sees.

🔴 **That was false for one commit, and the way it was false is worth keeping.**
`argparse`'s `allow_abbrev` defaults to `True`, so `--new-key` — the flag the
sentence above and every document about this command invite an operator to type
— was an unambiguous **prefix** of `--new-key-env`, and argparse silently bound
the key into the field meant for a variable *name*. The "is not set" message
then printed it back twice, once as `$<key>` and once inside a copy-pasteable
`export <key>=…`. Found in review 2026-08-26. The case asserting the key is
absent from the parsed namespace could not see it, because the namespace was
exactly the right *shape* and the wrong value was in the right field.
**Four controls ship, and each was measured over the same seven invocations:**

1. **`allow_abbrev=False` on this subparser**, which does not propagate from
   the parser above it — measured, a subparser inherits nothing.
2. **`--new-key` is declared** as a suppressed tripwire whose only action is to
   refuse, naming the flag and never the value. Without it,
   `allow_abbrev=False` *alone* leaves `--new-key K --new-key-env V` reaching
   argparse's own `unrecognized arguments: %s`, **which prints the key** —
   fixing the binding introduces a leak.
3. **`parse_args` refuses this one command's unrecognised arguments without
   them**, which closes what is left (`--newkey`, `--new-k`). Every other
   command keeps argparse's wording, because there a refused token is a typo
   and naming it is how it gets fixed.
4. **`--new-key-env`'s value must be an environment variable *name***
   (`[A-Za-z_][A-Za-z0-9_]*`) and must not be something `Settings` would accept
   as a key; either refusal prints nothing back. The second is not redundant:
   `openssl rand -hex 32` — the command this document recommends — emits a
   **legal variable name** whenever its first character is `a`-`f`, which is
   6/16 = **37.5%** of the time (measured over 100,000 samples: 37.6%), so a
   grammar check alone would have echoed better than a third of all real keys
   handed to the correct flag.

A well-formed name that cannot be a key is still echoed when it is unset,
deliberately — an operator who forgot the `export` needs to see which variable
was looked for, and such a name is not a secret.

⚠️ **Export it; do not add it to `.env`.** Measured
2026-08-25: an exported `USHER_NEW_SECRET_KEY` is invisible to `Settings`
(pydantic-settings' env source reads only the fields it declares), and the same
name written into `.env` makes **every** entry point fail
`usher_new_secret_key: Extra inputs are not permitted` — the `extra="forbid"`
failure the `USHER_COMPOSE_` namespace exists for, one section up — with the
new key rendered in the `ValidationError`'s `input_value=`.

**The new key is validated by `Settings`' own rules before the first row is
touched.** `min_length=32` and the placeholder rejection, reached by
constructing a real `Settings` rather than by a second copy of the two rules: a
rotation to a key `Settings` would refuse is a rotation that bricks the next
start, and that refusal must not arrive from pydantic at the next boot with
every credential already re-encrypted under it.

**Per-row commit, which is deliberately the opposite of `usher restore`.** One
transaction over N rows means an interrupted rotation leaves *every* row on the
old key while the operator has already changed their key — total credential
loss on the next start. Per-row commit leaves a **mixed** state, and a mixed
state is recoverable three ways at once:

- **It is diagnosable.** Fernet's authentication tag makes a wrong key a
  `PortDataMalformed` naming the ref, which `GET /admin/sources/{id}/status`
  already renders as an unreachable, unauthenticated source with a
  re-enter-your-credentials detail — the screen this degradation was designed
  to arrive at.
- **Re-running is the recovery, and it needs no ledger.** Every row is tried
  with the **new** cipher first and skipped if it already opens; only then is
  the old one tried. A second run over a half-rotated table finishes it, and a
  run over a fully-rotated one is a no-op reporting *N already rotated*. Trying
  the old cipher first is correct on a fresh table and **double-encrypts** every
  row a previous run moved — measured, and still perfectly readable, which is
  why the case that catches it asserts byte-identity rather than readability.
- **A row that opens under neither key is refused, named and counted**, the run
  exits non-zero, and the row is left exactly as it was — writing anything onto
  it would destroy the one copy a restored key could still have read.
- 🔴 **A run that refused *every* row is a different diagnosis and says so.**
  K8's drill measured it: an operator who changes `.env` before running the
  command makes `old_cipher` and `new_cipher` the same cipher, so every row on
  the previous key opens under neither and the report reads `refused N` with
  nothing written. Until 2026-08-26 the exit message told that operator their
  credentials *"must be re-entered"* — destroying working state to fix a problem
  that does not exist. `cli._rotation_refusal` now splits the saturated count
  (`len(refused) == report.rows`) from the partial one: the saturated arm names
  `USHER_SECRET_KEY`, says nothing was lost, and does not mention
  re-registration at all. [`docs/runbooks/rotation.md`](../runbooks/rotation.md)
  §0 is the operator-facing form.

⚠️ `PortDataMalformed` is **not** in `cli.OPERATOR_ERRORS` and this command does
not add it. `CredentialCiphertextStore` does not decrypt, so a row no key opens
never becomes an exception at all: it is a `None` and a counted refusal, which
is the per-command *handling*
[ADR-0026](decisions/0026-the-cli-boundary-names-families.md) permits as
distinct from the per-command *boundary* it rejects.

**The raw ciphertext is a second port rather than three more methods on
`CredentialStore`.** `api/deps.py::get_credential_store` returns the port
precisely so *"a caller written against this annotation cannot reach a method
`CredentialStore` does not have"*, and a `read_ciphertext` on that port would
put a credential blob one attribute access away from every route and both
services that hold one. Split, only the composition root that builds the
rotation service can name it — and `PostgresCredentialRotationStore` takes no
`secret_key` at all, so it moves bytes it cannot read.

## Failure and degradation

**A degraded subsystem narrows functionality; it never fails a request that
local state can answer.**

| Failure | Behaviour |
|---|---|
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong. |
| A refused availability sweep (a source that churns, or one Usher has only partly ingested) | ✅ M10 S8/S9: the sweep retracts **nothing** and the run records `FAILED` with the two numbers and the ceiling ([ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)); the catalog is unaffected and availability goes stale rather than wrong. `usher sync` now **exits non-zero** on any failed run and names `usher sync --allow-full-retraction`, which is the operator's action *if the removal was intended* — until 2026-08-19 the command printed the word `failed` and exited 0, so cron and CI saw success. `usher.sync.retraction.fraction{outcome="refused"}` is the series; it is recorded on **every** finished full walk, so a flat zero means "nothing was shed" rather than "no sweep ran". ⚠️ **On a *view* of somebody else's library a refusal can be the steady state rather than an incident** — refusing nightly is indistinguishable from having no sweep — and the thing that trips it first is Usher's own partial coverage, not the owner's deletions (measured: this deployment's one firing refused 60 of 180 at 33% after a bounded walk, with nothing deleted). If it recurs, check that the last full walk **completed** before reaching for the flag. |
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. Catalog unaffected. |
| Push socket drops | Backoff reconnect; delta reconcile on reconnect — ✅ **only when a completed walk gives that delta a cursor** (`USHER_PUSH_GAP_CLOSE`, above; issue #9). With none the lane refuses the walk, logs a WARNING naming the source and `usher sync`, and keeps the socket up ( a source with no completed item-lane run has no cursor, so its "delta" is a walk of the whole library that nobody asked for; see [03](03-sources-and-sync.md)'s Reconnect-delta row). The same refusal covers a push event **deferred** to a delta, so on such a source an oversized event is applied neither inline nor by a walk until that full sync has run. After N failures (`USHER_PUSH_MAX_CONSECUTIVE_FAILURES`, default 5) mark `supports_push = false` and lean on the nightly walk. **The failure counter resets on delivery, not on connection**, and the reason is a design that was *not* taken: **if** it reset on connection, a proxy that upgrades and then buffers would connect perfectly every time, a counter reset by connecting would never reach the ceiling, and this row would silently never fire ([ADR-0018](decisions/0018-push-health-is-a-message-ledger.md)). ⚠️ **That clause is a counterfactual about the rejected design and the M10 spec read it as a description of shipping code** (`docs/specs/2026-08-13-m10-hardening-design.md:155-157`, struck by S10). The row **does** fire, and the evidence is two-sided: `tests/unit/test_services_push.py`'s ceiling case drives the whole path end to end (`push_available == [False]` after `sleeps == [5.0, 10.0]`, the last failure hitting the ceiling without sleeping), and M5's final sweep measured the **inverse** mutation — `failures = 0` moved from delivery to connection — as failing **4** cases. ✅ **M10 S10**: when the ceiling is reached the lane's task finishes and the next `refresh()` **releases the adapter**, so the socket against a server this deployment does not own is closed rather than held for the process lifetime, `usher.source.push.delivering` stops publishing for that lane, and `GET /admin/sources/{id}/status` answers `push_available: null` ("not probed") rather than `false` off a dead ledger. The lane is deliberately **not** restarted — a refresh that replaced it would reconnect forever against exactly the buffering proxy this ceiling exists for. |
| Gap-closing delta larger than `USHER_PUSH_GAP_MAX_ITEMS` | ✅ M10 S6: the item walk stops at the ceiling (default **20,000 items** — 100 pages at `USHER_SOURCE_PAGE_SIZE`'s 200, ~10 minutes at the 6.04 s/page mean measured 2026-08-15, and under the 28,934 items a 30-day delta returned on the measured library; `0` is unlimited). The run records **`FAILED`**, never `COMPLETED`: `latest_completed_cursor` reads only completed runs, so a truncated run that completed would advance the cursor to its own start instant and everything past the ceiling would never be requested by any delta again — and **nothing in `src/` schedules the nightly full reconcile** that would otherwise cover it. **Nothing the walk saw is lost** (`_flush` commits per batch); what a ceiling costs is the cursor advance. One WARNING names the source, how far it got, the setting and `usher sync --kind full`, whose walk is what closes the rest. `usher sync --kind delta` and `POST /admin/sources/{id}/sync` are unbounded — the ceiling is the lane's, and the lane is the caller nobody typed a command for. **The item lane only**: the watch lane owns its own cursor and still walks whole after a truncated item walk, so this bounds one of the two lanes (see [03](03-sources-and-sync.md)). |
| TMDb 429 or down | Enrichment retries with jittered backoff. Stubs stay stubs; every other subsystem is unaffected. |
| TMDb key missing | Bootstrap Phase 3 skipped. Skeleton catalog and full-text search still work; semantic search degrades. |
| Provider image CDN unreachable | ✅ M9: catalog and every rendered card unaffected — an artwork reference is a row, not a fetch, so browsing, search and the home screen never touch the CDN. A **cold** image (no entry for that `(image id, rung)`) answers `503 source_unavailable` with `Retry-After`; a **cached** one still serves, because `GET /images/{id}` reads the disk before the network. The CDN needs no credential, so this row has no authentication arm **of its own** — which is not the same as saying a 401/403 cannot arrive here: it can, and it means something *in front of* the CDN refused (a proxy, a portal), which is why it belongs to the residual population rather than to a credential problem an operator can fix by rotating a key. An answer the proxy cannot serve splits in two: artwork this deployment declines to carry (an `image/svg+xml` logo, ~1 title in 17) is an ordinary `404 not_found`, and everything else — a 4xx, a body past `image_max_bytes`, a captive portal's HTML under a 200 — is `503 source_unavailable` with **no** `Retry-After`, since re-asking produces the same answer. ✅ **M10 F3: that second arm's honest status is a 502, [ADR-0030](decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)'s closed vocabulary has no code for one, and the amendment asking for one is now `Declined` — so `Retry-After`'s presence and absence **is** the contract between the two 503s rather than an interim.** Measured 2026-08-20 against the live CDN: the residual arm fired on **0 of 240** fetches (3 kinds × 20 stored rows × all 4 rungs, both controls firing), so its rate on this catalog is below **1.25%** at 95% confidence, the largest body seen was 16.5% of `image_max_bytes`, and every rung served every kind — including `logo` at `w1280`, the "rung withdrawn from a kind" case. ⚠️ **A captive portal's HTML is the one residual cause the measurement could not reach**: it is a property of a network in front of Usher, not of the CDN, so a healthy network cannot produce one and no sample size would have. ⚠️ **And a CDN 429 or 401/403 reaches neither arm** — `port_error_for` answers those with `PortRateLimited`/`PortAuthFailed`, which do not subclass `PortUnavailable`, so `GET /images/{id}` answers a bare `500` outside the envelope (measured 2026-08-20, `PortUnavailable` answering a `503` problem document as the control). Never observed live; not fixed by F3, which changed no behaviour. |
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

Telemetry is optional: with no OTLP endpoint configured no exporter object
is constructed at all and Usher runs normally.

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

✅ **M10 F2 adds three more body-only fields on exactly those terms** —
`lanes.crashed_sources` (lanes whose task has finished and will not restart),
`lanes.recovered_claims` (abandoned claims *this process* has taken back since
it started, `null` where it runs no worker and has therefore never asked) and
`lanes.recovered_at` — **none of them in the status code**, because a process
that has just taken back a dead peer's claims is doing its job and dropping it
out of a load balancer for saying so is the inversion this split exists to
prevent. The shape, the per-process bound and why the count is the one
`JobWorker.recover()` returned rather than a per-poll query are on `LaneReport`
(`src/usher/api/dto/health.py`), which is the wire contract. `usher work`, which
has no readiness route, prints the same running total in its pass line —
at startup and again whenever it changes.

The report is still degraded rather than binary, so a dashboard can
distinguish "down" from "running without Emby" — it just does so by reading
the body, which is what a dashboard does and what Kubernetes, Docker
`healthcheck` and a load balancer never do.

Lane state is free to report — `lanes.push` is the set of running lane
*tasks*, and `push_available` is an in-memory ledger of messages received,
not a probe — so readiness makes **no upstream request at all**. The shipped
compose healthcheck polls this endpoint every 2 s; the reason that matters is
the 503-takes-the-process-out-of-a-load-balancer argument above, not the price
of a probe, which [01](01-architecture.md) now measures at 0.1253 s rather
than at the 1–5 s this sentence used to cite. The on-demand probe
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
  can act on — `OSError`, `DBAPIError`, `httpx.HTTPError`,
  `ValidationError`, and since M8 the port taxonomy's transport half
  (`PortUnavailable`, `PortAuthFailed`, `PortRateLimited`) — and answers each
  with one line and exit 1; `usher --traceback <command>` re-raises.
  **`Exception` is deliberately not among them**, so a bug still gets its full
  traceback — and **`DBAPIError` reads `SQLAlchemyError` in every version of
  this document before 2026-08-19**, which was the same mistake one family
  narrower: `SQLAlchemyError` is also the base of `InvalidRequestError`, so the
  boundary answered `MissingGreenlet`, `PendingRollbackError` and
  `ObjectDeletedError` — bugs, all of them — with one line and no stack. It
  cost the diagnosis of the crash in issue #8 for a week. Ctrl-C exits 130
  rather than printing one. Neither is
  `UsherPortError` itself: an adapter translates its transport's failures
  before they cross, so `httpx.HTTPError` is unreachable behind a port and the
  three transport members had to be named — but `RepositoryConflict`,
  `RepositoryNotFound` and `PortDataMalformed` keep their stacks, because
  several of their raise sites are deliberate tripwires for bugs in Usher's
  own code. Why those families and not `Exception`, why the settings case is
  redacted, and the per-family evidence for the M8 widening are
  [ADR-0026](decisions/0026-the-cli-boundary-names-families.md).

### Scheduled work — two named jobs, no table, and off by default

⏳ **Designed by [ADR-0046](decisions/0046-the-scheduler-stores-nothing.md),
built by M10's J4.** Read the ADR before changing any of it; the contested part
is that the component holds no state at all.

Two jobs are registered, and the contract is a **name**, a **period**, a
`last_done()` and a `run()` — no crontab expression, no calendar, no timezone,
no dependency graph:

| job | what it runs | `last_done()` reads | period | shipped? |
|---|---|---|---|---|
| `search_queries` retention | `DELETE FROM search_queries WHERE at < :cutoff`, chunked, a commit per chunk | `min(min(search_queries.at) + window, now)` | 1 day | ✅ M10's J5 |
| the neighbour rebuild | `usher similar --rebuild`'s batch | `min(title_neighbors.computed_at)` | ⏳ set by its registration | ⏳ M10's J6 |

🔴 **`min(search_queries.at)` was named here as retention's `last_done()` and
cannot be one; the reading in the table is what replaced it.** `min(at)` is the
age of the **oldest surviving row**, written by the search path rather than by
this job, so after a prune it sits at the window's age and stays there — the job
reads as due on every tick forever, for any period shorter than the window, and
the period decides nothing. **A `last_done()` has to be a reading this job's own
runs move**, which is what
`usher.ports.scheduler.ScheduledJob.last_done` states.

**What retention maintains is the table's lower bound, and that is what a
completion time can be read off.** A successful run at instant *T* establishes
*"no row is older than T − window"*, so the invariant held at *T* and goes on
holding until the oldest surviving row itself falls out of the window — hence
`min(min(at) + window, now)`, *"the most recent instant this table was known to
hold nothing past its cutoff"*. A prune moves it and a new search cannot,
because a new row is the newest one. An **empty** table answers `now` rather
than `None`: nothing to prune is the invariant satisfied, where `None` would
mean *"never built, therefore due"* and would make an idle deployment prune on
every tick forever.

**So the period is how much expired data may accumulate, not how long a row is
kept**, and the two numbers are set in different places on purpose: the *window*
is `USHER_SEARCH_QUERY_RETENTION_DAYS` because it is a household's own history,
and the *period* is a property of the job. A day of expiry is **single digits of
rows** against a 10,000-row chunk, so the steady-state prune is one transaction.

⚠️ **This paragraph said "about 1,050 rows on this deployment" until 2026-09-07,
and that number was a burst divided by a span it did not arrive over.** The
14,978-row clone it came from holds **14,898 `surface = 'suggest'` rows written
on one day** — 2026-08-27, when J2's keystroke writer was exercised — leaving
**80** organic rows across the fortnight either side; re-measured 2026-09-07 the
organic rate is **5.6 rows a day**, with the live catalog at 5.7/day (109 rows
over 19 d 02 h) and `usher_wt_devdb` at 7.7/day (107 rows over 13 d 23 h). The
conclusion is unchanged and stronger. **The burst is not noise, though — it is
what the chunk size exists for**: a first prune after the suggest writer is
switched on, or after an outage, is the case where 10,000 is load-bearing, and
[PRD 10](10-telemetry-and-dashboards.md) carries both numbers together for
exactly that reason.

⚠️ **A failed retention run converges and a failed rebuild does not.** The
scheduler spaces retries and cannot bound *progress* — a batch that restarts
from page one redoes its work however far apart the attempts are — but a prune
is not such a batch: every committed chunk removes rows permanently and the
chunks go oldest first, so an interrupted run leaves progress the next one
keeps. The rebuild's resume is J6's.

**`USHER_SCHEDULER_ENABLED` defaults to `false`**, and turning it on is an
operator decision with a number attached. A fresh deployment has no embeddings,
so the first tick of an enabled scheduler would eventually start a walk nobody
asked for: measured from the artefact's own timestamps on this deployment's
catalog, the most recent completed rebuild took **3.58 hours over 132,442
seeds** (2026-08-19). And there is no mutual exclusion — the exclusion
`JobQueue` provides is a lock on a job **row**, and this component has no rows —
so a deployment running both the server and a separate `usher work` container
with the scheduler on in each would start that walk twice.

**A period is a minimum interval since last completion, not a wall-clock
schedule.** *"Every night at 3am"* is not expressible. An operator who wants a
wall-clock time runs **`usher schedule --once`** from their own cron, which is
the pre-M10 arrangement kept as a supported path rather than replaced — and it
is also the answer for anyone who wants the jobs without a long-lived process
holding them.

⚠️ **A scheduler that is on does not make the artefact complete.** `last_done()`
answers *when*, not *whether*: measured 2026-08-27, 877 of 133,319 embedded
titles carry no `title_neighbors` row at all, because they were embedded after
the last walk started, and `stale_neighbors()` reads **0** throughout — a
missing row has no fingerprint to disagree. The period is what eventually
covers a growing population; nothing here is a completeness guarantee.

### Backup — the asymmetry is the point

✅ **The split below is now generated from a manifest, and a test enforces
it.** `src/usher/db/backup_manifest.py` classifies **every table in the live
schema** — 29 of them, 7 precious, 1 partial, 20 rebuildable, 1 Alembic's own
— each with the reason it is where it is and, for a rebuildable one, the
command that reproduces it.
`tests/integration/test_backup_manifest_covers_the_live_schema.py` reads
`information_schema` after the real migration chain and fails in **both**
directions, so a table a future migration adds is a red rather than a
paragraph nobody remembered to update. **That guard exists because this
section drifted twice**, which is what the two ✅ M9 entries below record.

| Rebuildable from importers | Precious |
|---|---|
| Catalog, embeddings, search index, neighbour tables, cached images, curated rows, the payload cache, the genome, the job queue, the run logs | **Watch state**, users, source config, `source_credentials`, ✅ **`llm_calls`** (M8 — see below; it is rebuildable from nothing), ✅ **`row_provider_settings`** (M9), ✅ **`search_queries`** (M9) |

⚠️ **"Manual unmatched resolutions" was in that right-hand column for eight
milestones and is not a table.** It names two *columns* —
`media_items.title_id` and `media_items.episode_id`, which
`db/repositories/media_item.py`'s `attach_title` writes and
`api/routers/unmatched.py`'s resolve route reaches. Every other column of
`media_items` is rebuilt by the next source walk, and on the household this
project measures that is **1,126,789 rows to carry for the sake of a handful
of links**. So `media_items` is in neither column above: the manifest gives it
a third class, `PARTIAL`, and carries those two columns alone. It carries
**all** of them rather than only the operator's, because the schema has no
provenance column and `services/handlers.py`'s automatic match handler calls
the *same* `attach_title` as the route — "only the manual ones" is not a
distinction this schema can express. The harm is asymmetric, which is what
makes carrying all of them the right call: a link the match ladder would have
re-derived is re-derived to the same answer, and a link it would not re-derive
is exactly the operator's judgement. Restore writes one only where the
target's is `NULL`.

✅ **What those precious rows carry instead of a title id is now decided and
built** — `src/usher/db/backup_identity.py`, and
[ADR-0045](decisions/0045-a-backup-carries-natural-keys-not-ids.md) is the
argument. Five columns in the precious set name a title or an episode **by
id** and none of those ids survives a bootstrap boundary: `upsert_titles`
mints `new_id()` per staged row, so two catalogs built from the same IMDb dump
agree on every natural key and on no id at all. So an artifact carries
`imdb_id`, falling back to `(kind, tmdb_id)` and then to the raw UUID —
accepted **only** where the target already holds a title with that exact id,
which is what makes restore-into-the-same-database an ordinary lookup rather
than a second mode. An unresolved reference is a named refusal rather than a
`None`, and what restore does with one differs per table on purpose:
`watch_states` and `media_items` refuse the row and count it,
`search_queries.clicked_title_id` is set `NULL` and counted. 🔴 **The
coverage figure this design was drafted against does not survive
re-measurement**: on 2026-08-13 the live catalog held **0** titles with
neither provider id and on 2026-08-21 it held **6** (of 1,272,888, with 72
carrying no `imdb_id` against 13 eight days earlier), so the raw-id rung is
exercised by real rows rather than being defensive. ✅ **Counted in a real
artifact on 2026-08-25 rather than inferred from the catalog**: `usher backup`
against the live database carried **16,819 title references naming 7,581
distinct titles, of which 602 references — 3.6% — resolve by nothing but the
raw id**, because those same 6 unkeyed titles are referenced many times each.
A rung reached by 6 rows in 1.27 M reads as negligible from the catalog and is
one carried reference in 28 from the artifact's side, which is the number that
matters to a restore. `curated_rows` is never
carried, and its `uuid[]` with no foreign key is why: it is the one
precious-looking table where a wrong id fails nothing at all.

✅ **The precious set is a handful of small tables — counted rather than
asserted since 2026-08-25 — and the command that carries them is `usher backup`
(M10 Group K, K3) rather than a documented `pg_dump`.** Re-measured read-only
against the live database on **2026-08-25**, and two of the eight moved enough
to matter: **1** user, **1** source, **1** credential row, **3,347** watch
states, **0** `llm_calls`, **1** row-provider setting, **89** search queries and
**10,819** linked media items of 13,539 — **14,259 rows**, which is the whole
artifact. ⚠️ The figures K1 and the K3 plan were drafted against on 2026-08-13
read **0** watch states and **180** links, so the reference-rewriting ladder
`usher backup` exists for was designed against a table with nothing in it. The
`pg_dump` half is measured
2026-08-13, and the reason is that the documented alternative *cannot be run
from the container this project ships*. The runtime image is
`python:3.13-slim` and carries neither `pg_dump` nor `psql`, so an operator
following that advice would have to reach the binary inside the **Postgres**
container, which Usher does not own; adding `postgresql-client` to the runtime
stage costs **+62.1 MiB, +18% on a 359 MB image**, of which
`/usr/lib/postgresql` is 3.9 MiB and the rest is libpq, OpenSSL, readline and
Perl. Either way the point stands: disaster recovery becomes a short restore
plus a background rebuild instead of a crisis — ✅ and since K4 that sentence
names a command, `usher restore`, whose own section is below. State this loudly
in the README — it is the difference between "lost everything" and "lost an
afternoon of indexing".

**The artifact is gzip-compressed JSON Lines: a header object, then one object
per row carrying `table` and `row`.** Four properties earn that over any
Postgres-native format, and the first is the one none of them has — **every
reference is rewritten on the way out**, which `pg_dump -t watch_states` cannot
express because there is nowhere in a custom-format archive to put a natural
key. The other three: the *format* streams (the seam is
`usher.db.staging.raw_connection` plus asyncpg's `copy_from_query`) — ⚠️ **the
shipped writer does not**, and holds all eight tables in memory before writing
a byte, which is affordable because the manifest keeps the carried set at
14,259 rows and for no other reason; an operator can read it, which matters
because this file is the only copy of the money ledger and of a household's
history; and it survives a Postgres version change, where `pg_dump -Fc` does
not restore into an older server.

**The destination is written through a scratch sibling and `os.replace`d into
place**, so a run that fails part-way leaves the previous artifact intact
rather than replacing it with a truncated one — and a truncated gzip
decompresses cleanly up to the point it stops, so the failure it prevents is
silent. The guarantee is against a failed run and not against a power cut:
`os.replace` is atomic with respect to readers, and nothing `fsync`s.

**The header carries two stamps and only one of them is enforced by refusal.**
`schema_revision` is Alembic's head as the *database* reports it, read through
`usher.db.migrations.status.database_revision` — the same function
`/health/ready`'s `_check_migrations` compares against `code_head_revision()`
to answer 503, so *"the app refuses to serve on a schema mismatch rather than
guessing"* and *"restore refuses rather than half-applying"* are one definition
rather than two. `generated_at`, `manifest_version`, `usher_version` and the
per-table row counts are provenance and a self-check: the counts are `len()` of
what was written rather than a `count(*)` taken beside it, which is what lets a
restore read a short table as a truncated file rather than as a race.

⚠️ **`source_credentials` is carried as ciphertext and `usher backup` does not
decrypt it** — `build_cipher` is not called on that path at all. So **an
artifact restored into a deployment holding a different `USHER_SECRET_KEY`
restores credentials nobody can read**, and the command says so in one sentence
on every run rather than behind a flag, because an operator who learns it at
restore time learns it too late. The degradation is the correct one: Fernet's
authentication tag makes it a diagnosable `PortDataMalformed` naming the ref
rather than garbage, and `GET /admin/sources/{id}/status` already renders that
as *re-enter your credentials* below. **Keep `USHER_SECRET_KEY` with the
artifact.**

#### Restore — one transaction, four refusals, and three counts (K4)

✅ **`usher restore <artifact> [--dry-run]` is built** —
`src/usher/services/restore.py` owns the file and
`db/repositories/backup.py`'s `PostgresRestoreRepository` owns the merge.

⚠️ **Its normal path is not an empty database, and reading it as one produces
a command that cannot work.** `watch_states.title_id` is `ON DELETE RESTRICT`
(ADR-0010; re-read off `pg_constraint` 2026-08-25), so into an empty catalog
the load-bearing table's every row fails its foreign key. What this document
promises two paragraphs up is the honest description and is what ships: **a
short restore plus a background rebuild.** The importers rebuild the catalog
(`usher bootstrap --phase all`, then `usher sync`, then `usher work`), and
restore lands the precious rows on top of it. Verified against a rebuilt
catalog in `tests/integration/test_restore.py`, including the case that
re-mints every title id between the backup and the restore — which is what a
bootstrap does, and the only reason the natural keys exist.

✅ **Both statements are now a drill rather than a design** — K5, 2026-08-25,
against a scratch `pgvector/pgvector:pg17` with the development database read
once and never written. Into an **empty** catalog the real artifact refuses
**14,166 of 14,259 rows** and commits nothing; into a **rebuilt** one, restore
is **5.06 s**. The operator-facing sequence is
[`docs/runbooks/restore.md`](../runbooks/restore.md) and its clock is
[`docs/runbooks/disaster-recovery.md`](../runbooks/disaster-recovery.md), both
written from that transcript.

**All four runbooks this section asks for — restore, upgrade, disaster recovery
and rotation — are indexed at [`docs/runbooks/README.md`](../runbooks/README.md).**

🔴 **The drill refuted one thing the design did not anticipate, and it is the
raw-id rung meeting `media_items`' `REFUSE`.** The 6 titles carrying neither
provider id hold **304 `media_items` links**, so a *correctly rebuilt* catalog
still refuses the whole file — rolling back the household, the source, its
credential, 3,347 resolved watch states and 89 search queries with it. The
control is decisive: the same unfiltered artifact into a catalog holding the
**original** ids restores with **0 refusals**. Two further consequences, both
recorded rather than fixed: a rung-3 refusal is *not* actionable by the
command's own *"enrich or import what the lines above name"*, because a stub
with no provider id is in no dump; and the rows that cost the whole restore are
the ones this document already calls re-derivable by the next source walk. The
runbook's escape is a one-line filter over the artifact, which is design
property 3 — *an operator can read it* — earning its keep.
`.claude/rules/db-and-sql.md` holds the measurements.

**Four refusals, in order, all before any write, and only the fourth is a
report.** (1) an artifact that is not readable or is truncated — including a
gzip member that ends early, which raises a bare `EOFError` and is therefore
*not* an `OSError` the CLI boundary would have caught; (2) a **schema
mismatch**, comparing the header's stamp against `database_revision` and
naming both values, the shape `/health/ready` already logs — against the
*database's* revision and never `code_head_revision()`, because a container
whose code is ahead of its database is a broken deployment that `/health/ready`
is the thing to report; (3) a `table` key the manifest does not classify,
which is what an artifact from a later schema looks like; (4) **unresolved
references, collected across the whole file and reported together**. A restore
that stopped at the first missing title tells an operator to enrich one title;
one that reports 41 tells them the catalog is not finished.

**One transaction for the whole file.** One session from `cli._session_for`,
one commit at the end, and a rollback otherwise — so an unresolved reference
in the last row rolls back the first, and *"refuses rather than
half-applies"* is a property of the code rather than a promise. ⚠️ **The
consequence is that the failure mode of a very large artifact is memory**, and
the bound is stated rather than discovered: the whole file is parsed before
anything is written and every row stays in one open transaction until the end.
The precious set is small by construction — 14,259 rows measured here — and
`--dry-run` resolves everything, prints the identical report and commits
nothing, which is how an operator learns what would be refused without holding
a transaction open while they think about it.

**The report separates written, already present, nothing-to-write-onto,
skipped-as-unresolvable, and refused**, per table, with the refused list naming
the keys that were looked for. Five numbers rather than one, because
*"restored 9 rows"* over an artifact holding 50 is the failure the command
exists to make visible. A run with any refusal exits non-zero, `usher sync`'s
precedent.

⚠️ **Two of those five were one number called *skipped* until K5's drill
printed it.** `media_items 0 written / 10,515 already present` against a
`media_items` table holding **zero rows**: its merge is an `UPDATE` over a row
the source walk creates, so *"the target already holds this link"* and *"there
is no row here at all"* both write nothing and read identically — while being
opposite instructions to an operator. The second is the **normal** state of a
first restore into a rebuilt deployment, because `media_items` rows come from a
walk and the walk needs the `sources` row the artifact carries.

**The refused list is capped at 20 named rows with an exact tail.** The same
drill printed a **14,176-line** report restoring into an empty catalog.
*"Every refusal is named and none is summarised away"* is right at 41 and
unusable at 14,166; the per-table counts are computed from the whole list and
stay exact whatever is printed, so *"how bad, and where"* survives the cap.

✅ **The header's per-table row counts are a truncation gate**, compared against
the body before any write. 🔴 Two paragraphs in `src/` described that check in
the present tense for a milestone before it existed, and the drill measured the
gap: an artifact whose header claimed 10,819 `media_items` over a body holding
10,515 restored with **0 refusals and exit 0** — a subset applied and reported
as success. The format is what makes the failure ordinary rather than exotic:
the artifact is gzip'd JSON Lines *so an operator can read and edit it*, and
every hand-edit that drops a line leaves the header saying how many there
should have been.

### `--skip-unresolvable`, and why the default does not move

🔴 **A correctly rebuilt catalog refused this deployment's own artifact.**
Measured 2026-08-25: 6 titles of 1,272,891 carry neither an `imdb_id` nor a
`tmdb_id` — all 6 `series` stubs the ingest ladder created — and they are named
by **304 `media_items` link rows**, one per episode file. `media_items`'
unresolved rule is `REFUSE` and restore is one transaction, so the household,
the source, its credential, **3,347 resolved watch states** and 89 search
queries were all written and all rolled back for 304 links. The control that
makes this K2's third rung rather than a defect: the *same* artifact into a
catalog holding the original ids restores with **0 refusals in 5.06 s**.

The rows that cost the restore are the rows whose loss costs nothing — K1's own
argument for carrying every link is that *"a link the match ladder would have
re-derived is re-derived to the same answer"*, and an unmatched stub's link is
exactly that.

**`usher restore --skip-unresolvable` drops those rows and commits the rest.
The default is unchanged and is not weakening.** *"Refuses rather than
half-applies"* is this command's headline guarantee, so the escape is an
operator explicitly accepting a loss rather than a heuristic the command
applies for them, and the dropped rows are counted in a bucket of their own —
never folded into *already present*, never written as a null. It composes with
`--dry-run`, which is how an operator learns what the trade costs before taking
it.

⚠️ **It covers exactly the references the importers rebuild.** A household the
target does not hold, a source colliding on a name, and a credential whose
source is absent all still refuse with the flag set: none of them is *"this
catalog is at a different bootstrap phase"*, and no `usher sync` re-derives any
of them. Skipping a household would silently drop every watch state in the file
— the exact loss this command exists to carry, arriving through the escape
hatch built for the opposite case.

**"Insert" is the wrong rule for five of the eight carried tables**:

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

⚠️ **The `sources` refusal cannot lean on the database, and that asymmetry is
easy to assume away.** Measured on the live schema 2026-08-25: `pg_constraint`
for `sources` holds **only** `pk_sources PRIMARY KEY (id)` and the count of
unique indexes on `name` is **0** — so two sources pointing at one server is a
state this schema permits, an `ON CONFLICT (name)` would not compile, and the
refusal is an explicit read with a case of its own. `users` really does have
`uq_users_name`, which is why its rule is one clause and this one is three.

**Restoring the same artifact twice is a no-op on the second run** — every
table's count identical, asserted table by table — because that is the
operator's instinct after a partial failure. It is what `DO NOTHING` on
`llm_calls` buys, and what the `IS DISTINCT FROM` guards on the two upserts
buy: the second run reports `skipped` rather than claiming to have written
3,347 rows that did not move.

**M7 added five tables and four of them are rebuildable, which is worth the
detail because "everything is rebuildable" is the kind of claim that is true
right up to the table it is not true of.**

| Table | Rebuildable? | From what, at what cost |
|---|---|---|
| `people`, `credits`, `collections` | **yes, with no network call at all** | `raw_payloads`, via `usher derive --backfill` ([03](03-sources-and-sync.md)'s stage 5). This is M4's boundary call 2 paying off: **the payload cache is the backup** |
| `user_taste` | **yes** | a mean over embeddings of the household's watch states. It carries its own fingerprint (`model_name` + `source_watermark`), so a missing row is *indistinguishable from a stale one* and is recomputed by the same predicate rather than restored. ⚠️ **This cell read *"as of M7 nothing in `src/` calls `TasteService.centroid`"* until 2026-08-21 — the same false absolute the paragraph below already corrects, left standing in a second place.** What is true is narrower: no writer on the **request path**, so the table is empty on a default deployment and not on one that has run a curation with an embedder configured — see below |
| `title_neighbors` | **yes** | `usher similar --rebuild`, and `blend_fingerprint` is what tells a restored table from a current one |
| **`genome_scores`** | **yes, but only from upstream** | re-download `ml-latest.zip` and re-run `bootstrap --phase movielens`. Frozen for three years, so reproducible in practice — **and not guaranteed**: GroupLens can withdraw or replace the archive, and then it is not rebuildable at all |

So the honest backup statement is that **`raw_payloads` and `watch_states` are
the load-bearing rows**, and `genome_scores` is the one M7 table whose
recreation depends on a third party still serving a file. It is not in the
precious column either, because a dump of it is a redistribution of MovieLens
data — permitted by `ml-latest`'s licence ([04](04-catalog-bootstrap.md)) and
still not something this project's own rule 1 does.
**That risk is accepted knowingly, and the manifest records it in the entry's
own reason string**: if GroupLens withdraws the archive, `genome_scores` and
`genome_tags` are not rebuildable at all — and carrying 15,565 MovieLens
vectors in a backup artifact is precisely the object
`tests/unit/test_no_third_party_data.py` already refuses over `src/`, one
directory away. This is the same refusal, not a bet on probability.

⚠️ **`raw_payloads` stays rebuildable, and it is the closest call in the whole
classification.** Against carrying it: **995 MB** — the third-largest relation
in this database at the 2026-08-13 reading, behind `title_neighbors` (1140 MB)
and `titles` (1050 MB) — which takes the artifact from the kilobytes the
precious set weighs on this deployment to a file an operator will not keep;
and it is third-party TMDb payloads verbatim, so the rule-1 argument bites
harder here than it does on the genome. For carrying it: M9's S3 measured
**130,334 requests over 1.98 h** to fill it, against a server this project does
not own. The ruling is rebuildable, with that cost stated in the manifest and
✅ in the runbook ([`disaster-recovery.md`](../runbooks/disaster-recovery.md)'s
clock, which is where the 1.98 h lands in the recovery sequence), and
**`usher backup --include-payloads` is deliberately
not built** — a flag that makes the artifact redistribute TMDb payloads is a
licensing decision rather than an operator convenience.

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
deliberately holds none of. ⚠️ **The sentence that used to stand here — *"So
`TasteService.centroid` has no caller in `src/`"* — was the true claim above it
escalated one hop into a false absolute, and has been false since M8:
`services/curation_pool.py:174` calls it, and the guard immediately above that
call exists *because* it writes.** (That citation read `:173` until 2026-08-21,
which is the last line of the comment block above the call — a correction to a
correction, and the reason the manifest's own reasons cite files rather than
line numbers.) What survives is the narrow claim: no writer
on the **request path**, so the table stays empty on a default deployment; what `TasteService` *is*
called for is `genre_affinity`, which needs no embedder and no centroid. The
table, its fingerprint and its written refusal are all built and tested;
the consumer is M9's, with the ranking terms
[05](05-search-and-similarity.md) names. Recorded here rather than left for an
operator to discover from an empty table.

✅ **Row provider enable/disable belonged in the *precious* column the day it
existed, and this paragraph spent a milestone saying so while the table above
did not list it.** `row_provider_settings` exists — `m09a` creates it, M9's
E1/E2 shipped its repository and `PUT /admin/rows/providers` — and it is
operator-authored state in the database, like source config, which no importer
restores. It is precious, it is in the table above, and **the table is no
longer maintained by whoever remembers**: it comes from
`src/usher/db/backup_manifest.py`, which classifies all 29 tables and is
enforced against the live schema in both directions by a test.

🔴 **The second drift is worse, because nobody argued either way about it.**
`search_queries` — also `m09a`, also M9, nine columns — appeared **nowhere** in
this section. It is not rebuildable by anything: it is the record of what a
household typed and what it then played, and [09](09-roadmap.md) scopes issues
#15/#16 *"Post-v1 unless M9's `search_queries` supplies a real evaluation
set"* — a plan that depends on the table surviving. Losing it costs the only
evaluation set this project will ever have, and nothing can re-derive it. It is
precious.

**The general form, and it is worth separating "right by accident" from
"wrong".** M9 added four tables and this section was updated for none of them.
Two of the four land in the correct column anyway, because the left column's
*"search index, cached images"* happens to describe `title_search_names` and
`images` — but that phrase was written in commit `860b086` (2026-07-28, the
original 07/08 landing, before M1 shipped), so it predates both tables by nine
milestones and classifies them right by accident. The other two are the two
above. **A prose table is updated by whoever remembers; the two tables nobody
remembered are the two that were wrong.** That is the argument for the
manifest, and it is why the enforcement is a test rather than a convention.

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
| **Running the `m09e` migration** | 🔶 The only entry here that *frees* space, and the only one whose settled figure is unknown. It deletes every row of `title_embeddings` (130,673, in a 278 MB relation), `user_taste` and `title_neighbors` (3,266,175) and rebuilds the HNSW index empty. It runs no `VACUUM`, so the dead tuples are still on disk when it returns, and the catalog then re-grows: measured 2026-08-13, the backfill of 130,720 titles took **105.9 minutes** and settled at **707 MB** of relation and **340 MB** of index. 🔴 **`usher similar --rebuild` is the expensive half and its cost changed by more than the width did: 594.7 ms/seed against 36.50 at `halfvec(384)`, i.e. a full 130,720-seed walk of 21.6 hours against 80 minutes.** ⚠️ **Both figures are `m09e`'s and `m09f` repaired them** — moving every `halfvec` column to `PLAIN` storage took the exact scan back to **91.7 ms/seed**, so the completed walk measured **130,720 seeds in 11,981 s = 3.33 hours**, not 21.6. The overnight-job conclusion below survives; the number that motivated it does not. ⚠️ **And 3.33 h is 2026-08-13's; budget a recovery against `### Scheduled work`'s figure above** — the walk this deployment most recently completed is 12,884 s over 132,442 seeds, **97.3 ms/seed**, on 2026-08-19. Both are real, this row records what `m09f` bought, and that section records what to plan against. `nearest_for` runs under `enable_indexscan = off` by design, so it is an exact scan whose working set now exceeds both `shared_buffers` and this host's L3. Plan the rebuild as an overnight job, not a follow-on step ([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)). |
| HNSW (`halfvec`) | 🔶 **~1.5 GB at full embedding coverage was projected at `halfvec(384)` and is now a floor.** Measured immediately before `m09e` on 2026-08-13: **146 MB** of `ix_title_embeddings_hnsw` over **130,673** embeddings, inside a **278 MB** `title_embeddings` total relation. `m09e` widened the lane count to 1024 — 2,048 bytes a vector against 768 — and the rebuilt figures, measured 2026-08-13 after 130,720 titles re-embedded through `bge-m3`, are **340 MB** of index inside a **707 MB** relation: **2.33× and 2.54×**, both under the 2.67× the lane count alone predicts, so the graph and the row headers amortise a little. Extrapolating the old ~1.5 GB projection by the same 2.33× gives ~3.5 GB at full catalog coverage — an extrapolation, and labelled as one. |
| Image cache | Grows with use, **without bound**. ⚠️ *"capped by a configurable LRU ceiling"* was wrong from the day it was written: there is **no eviction** (`services/images.py:7` says so in as many words) and no ceiling setting. `image_max_bytes` is a **per-image** 5 MiB refusal, not a cache ceiling. Nothing reclaims this directory. |
| Usher process | ~500 MB–1 GB, plus ~200 MB for the embedding model — **the model half applies to the `fastembed:` runtime only**, and was measured for `bge-small-en-v1.5`. Under `openai:` no model is loaded in this process at all; the memory is the inference server's. |
| Embedding model | 🔶 **~130 MB on disk was `bge-small-en-v1.5`'s measured download.** The shipped default is now `fastembed:BAAI/bge-large-en-v1.5`, which fastembed 0.8.0 declares at **1.2 GB** against bge-small's 0.07 — the price of the 1024-wide column, and the reason this row is called out rather than quietly edited ([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)). Its download has not been re-measured, and it is **0 on the `openai:` runtime**. |

Tuning that matters: `maintenance_work_mem` high enough to avoid the
`hnsw graph no longer fits into maintenance_work_mem` notice during index
builds, `max_parallel_maintenance_workers = 7`, and GIN `fastupdate = off`.
