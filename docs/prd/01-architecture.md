# 01 — Architecture

## Shape: modular monolith, hexagonal core

One deployable application. Ports-and-adapters internally so the pieces are
independently testable and swappable, without the operational cost of splitting
services for a household-scale deployment.

```
   [household clients]   [Alfred]   [Home Assistant]
              │              │            │
              └──────────────┴────────────┘
                             │  HTTP + SSE
┌────────────────────────────▼──────────────────────────────┐
│ Usher                                                     │
│                                                           │
│  api/         routers · dependencies · DTOs               │
│  services/    catalog · ingest · match · enrich ·         │
│               search · rows · curation · watchstate ·     │
│               bootstrap                                   │
│  domain/      Pydantic models — the canonical language    │
│  ports/       SourceAdapter · MetadataProvider ·          │
│               SearchIndex · Embedder · LLMClient ·        │
│               TitleRepository · Row · RowProvider         │
│               (all ABCs)                                  │
│  adapters/    emby/ · tmdb/ · bulk/ ·                     │
│               search/ · embedding/ · llm/                 │
│  jobs/        priority queue · schedulers · workers       │
│  db/          SQLAlchemy 2.0 async · Alembic · repos      │
└────────────────────────────┬──────────────────────────────┘
                             ▼
                       [PostgreSQL]
             canonical catalog · search · vectors · job queue
```

**`adapters/` subdirectories are named for the upstream service when a
port's implementation talks to one nameable external service** (`emby/` →
`SourceAdapter`, `tmdb/` → `MetadataProvider`) **and for the capability
otherwise.** That covers two different reasons, not one: a port with more
than one implementation (`bulk/` → `BulkDataset`'s dataset importers,
`search/` → `SearchIndex`'s Postgres/Meilisearch pair) obviously can't be
named for a single service — but `embedding/` and `llm/` are capability-named
too, despite one implementation each, because neither implementation is
itself a single external service to name: **`fastembed` runs in-process
against a local ONNX conversion of a BAAI checkpoint** — a Qdrant library
serving a third-party conversion of somebody else's weights, which is three
names and no upstream service at all — and the LLM client's upstream is
whatever `USHER_LLM_BASE_URL` points at, which is a setting rather than a
name. (That second half read *"`litellm` is itself a multi-provider
abstraction, not one upstream"* until M8 declined the dependency
([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md)); the
conclusion is unchanged and the reason is now a property of the shipped code
rather than of a library.) (This example read
`sentence-transformers` until M6 replaced the runtime; the argument is
unchanged and the substitution makes it stronger. See
[ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md).)

M6 shipped `adapters/search/postgres.py` and
`adapters/embedding/fastembed.py` under exactly that rule. **`adapters/postgres/`
does not exist and must not be created**: it would put a `SearchIndex` and a
repository in one directory, and the `### adapters/search/ vs db/repositories/`
section below exists because those are not the same kind of thing.

**Deployment:** `compose.yml` with `usher` + `postgres`. One stateful service.
**There is no `meilisearch` service** — the sentence here used to say one
existed behind a feature gate, and none has ever been in `compose.yml`. What
exists is the gate itself, which is a measurement with a decision attached
([ADR-0002](decisions/0002-postgres-first-search.md)). **It ran on 2026-08-03
against a real 1,271,138-title catalog and it failed** — 27.8% recall@5 on
2–4-character names against a bar of 0.75, and a p95 of 211 ms against a 50 ms
as-you-type budget that no configuration clearing the recall half comes closer
to — **and no `meilisearch` service was added anyway**,
because the answer the gate produced is a two-tier suggest owned by M9 rather
than a second engine ([09](09-roadmap.md)). If one is ever taken, it is added
behind the `SuggestIndex` port alone — that being the whole of
[ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md).

## Layering rules

These are the invariants that keep Emby out of everything:

1. **`domain/` imports nothing from `adapters/`, `db/`, or `api/`.** It is pure
   Pydantic models and value objects.
2. **`services/` depends only on `domain/` and `ports/`.** A service never
   imports an adapter; it receives one. Repositories are ports too, for the
   same reason — see [ADR-0009](decisions/0009-repositories-are-ports.md).
3. **`adapters/` implement `ports/` and may import `domain/`.** They translate
   foreign shapes into canonical ones at the boundary. Raw Emby or TMDb JSON
   never escapes its adapter package.
4. **`db/` models are separate from `domain/` models.** Repositories translate,
   and implement the repository ports declared in `ports/` (e.g.
   `TitleRepository` — [ADR-0009](decisions/0009-repositories-are-ports.md)).
   This costs a mapping layer and buys the freedom to shape tables for query
   performance without deforming the domain language.
5. **`api/` maps domain models to response DTOs.** Wire format is versioned
   independently of internal models.

Import discipline is enforced in CI (`import-linter` contracts), because
layering rules that are only documented become suggestions. **Eight contracts
as of M8, and the eighth is a rule the first seven structurally could not
reach.** Contracts two and three are sourced at `domain`/`ports`/`services`,
so the indirect chain that catches a *core* module reaching `usher.composition`
does not exist for a router — and `usher.api` is itself a composition root, so
it is allowed to reach `db/` and `adapters/` directly. A router doing
`from usher.composition import build_curation_service` therefore passed all
seven, ruff, mypy and both suites; planted and measured. The eighth forbids
`usher.api.routers` from **naming** `usher.composition`, `usher.services.
curation` or `usher.ports.llm`, and it needs `allow_indirect_imports = true` to
say only that: every router imports `usher.api.deps`, which imports the wiring
on purpose. A router may *reach* the wiring through a dependency and may not
*name* it — which is what makes [07](07-client-api.md)'s *"the route holds no
`LLMClient`"* a property of the build rather than of a review.

## Ports are ABCs, not Protocols

All ports are `abc.ABC` with `@abstractmethod`. Rationale and the trade-off we
accepted: [ADR-0001](decisions/0001-abc-over-protocol.md).

Practical consequence: a shared `BaseHTTPAdapter` carries the httpx client
lifecycle, retry/backoff, and rate-limit handling that the Emby and TMDb
adapters both need, instead of each reimplementing it.

**How much of that has actually been built, and which upstreams are
deliberately not throttled.** `src/usher/adapters/http.py` is the piece that
exists — `retry_after_seconds`, `decode_json`, `port_error_for`,
`UNTRANSLATED_FAILURES`, and since M10 the outbound gate `_MinInterval` with
its per-process `SourceGateRegistry`. The client *lifecycle* is still
per-adapter, because `CachedDatasetFile` is handed a client it does not own
while `EmbyAdapter` owns one per source. M10's S3 enumerated every outbound
call site under `adapters/` by grep rather than by memory and recorded a
limiter decision for each.

**The unit counted is the module**, and the table below has one row per module.
**Nine modules** under `src/usher/adapters/` dial an upstream: **eight over
httpx**, between them **sixteen call sites**, and a ninth — `emby/push.py` —
over `websockets`. *Upstreams* is a smaller number however it is counted, which
is why this is not a count of them: `tmdb/client.py` and `tmdb/provider.py` are
one host, and `/embywebsocket` is the same machine as the media source. Three
of the nine are paced; six deliberately are not.
`tests/unit/test_outbound_call_sites.py::test_the_module_census_is_the_one_the_records_quote`
asserts all four figures off its own table, and
`::test_prd_01_prints_the_census_this_table_computes` reads this paragraph and
the table below back out of this file, so neither can drift from the tree
without a red.

⚠️ **The call-site figure was fifteen until 2026-08-19 and it was short by
one.** The AST scan behind it enumerated six of `httpx.AsyncClient`'s eleven
request-issuing methods, omitting `put`, `delete`, `patch`, `head` and
`options` — and `bulk/download.py`'s `CachedDatasetFile.revision` has issued a
`HEAD` per dataset since M2. The module census, the paced count and the decline
count are unchanged; only the call-site total moves. The scan now carries all
eleven verbs, which is also what a **write-back** adapter at the `SourceKind`
seam would be spelled with.

| module → upstream | limiter |
|---|---|
| the configured media source (`emby/session.py`) | the per-source minimum-interval gate, `USHER_SOURCE_REQUESTS_PER_SECOND` ([ADR-0039](decisions/0039-the-outbound-limiter-is-per-source-and-spaces-requests.md)) |
| `api.themoviedb.org` (`tmdb/client.py`) | `_TokenBucket` at `USHER_TMDB_REQUESTS_PER_SECOND` ([ADR-0005](decisions/0005-bulk-bootstrap.md)) |
| `api.themoviedb.org` (`tmdb/provider.py`, six call sites) | the bucket above — this module holds no client of its own |
| `/embywebsocket` (`emby/push.py`) | **none** — a socket held open is not a request; the reconnect *backoff* is its limiter |
| `image.tmdb.org` (`images/provider.py`) | **none** — the CDN publishes no limit and the cache is the bound ([ADR-0032](decisions/0032-the-image-proxy-clamps-to-a-ladder.md)) |
| the IMDb/TMDb/MovieLens dataset hosts (`bulk/download.py`, two call sites) | **none** — one streamed file per dataset plus one `HEAD` for its revision, not a request stream |
| `query.wikidata.org` (`bulk/wikidata.py`) | **none** — a bootstrap phase run by hand, chunked and sequential, not a lane |
| `USHER_LLM_BASE_URL` (`llm/openai_compatible.py`) | **none** — `curate` is capped at 1 in flight and [06](06-rows-and-recommendations.md) budgets one completion per household per day |
| `USHER_EMBEDDING_MODEL`'s endpoint (`embedding/openai_compat.py`) | **none** — `index` is capped at 1 in flight |

**Six of the nine get nothing, and every one of the six has a stated reason
rather than an omission** — `emby/push.py` included, whose decline S3 recorded
only in the test table and which now carries it in its own module docstring.
That is as much the deliverable as the wiring is,
because `.claude/rules/ports-and-error-taxonomy.md` records what happens when a
decision about an upstream is left implicit. Each reason is written beside the
code, and `tests/unit/test_outbound_call_sites.py` holds the same table closed
against an AST scan, so **a new adapter with a new outbound call is a red rather
than a discovery**.

```python
class SourceAdapter(ABC):
    """A backend that holds playable media."""

    @property
    @abstractmethod
    def source_id(self) -> uuid.UUID: ...
    @property
    @abstractmethod
    def supports_push(self) -> bool: ...
    @abstractmethod
    async def verify(self) -> SourceStatus: ...
    @abstractmethod
    def list_items(self, since: datetime | None) -> AsyncIterator[SourceItem]: ...
    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None: ...
    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]: ...
    @abstractmethod
    def watch_state(self, since: datetime | None) -> AsyncIterator[SourceWatchState]: ...
    @abstractmethod
    async def get_watch_state(self, external_id: str) -> SourceWatchState | None: ...
    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None: ...
    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise SourceNotSupported; the
        reconciler covers them."""
    @abstractmethod
    async def aclose(self) -> None: ...
```

Note `list_items` and `watch_state` are plain `def`, not `async def` — they return an
`AsyncIterator` directly rather than being coroutines that produce one. The full
contract each method promises (ordering, `since` inclusivity, duplicates, must-raise
rather than truncate) lives on the real ABC in `src/usher/ports/source.py`; this sketch
shows shape, not the whole docstring.

`watch_state` walks and `get_watch_state` fetches one item, and they are two methods
rather than one because they are not equally truthful: a listing may be lossier than
an item route, so the walk is permitted to report play history as absent while
`get_watch_state` is not ([ADR-0014](decisions/0014-absence-is-not-zero.md)).
`verify` returns a `SourceStatus` rather than a bool, because
"reachable but the credentials are wrong" and "unreachable" are different answers
`GET /admin/sources/{id}/status` has to render.

Other ports follow the same pattern:

| Port | Implementations (v1) |
|---|---|
| `SourceAdapter` | `EmbyAdapter` |
| `MetadataProvider` | `TmdbMetadataProvider` |
| `BulkDataset` | `IMDbDumps`, `TMDbIdExport`, `WikidataCrosswalk`, `MovieLensGenomeDataset` — **the last shipped in M7** (`adapters/bulk/movielens.py`, `bootstrap --phase movielens`), and it is back in this table having been removed from it in M6 for not existing. Removing it was right then; restoring it with an implementation behind it is the same discipline in the other direction |
| `SearchIndex` | `PostgresSearchIndex` (`MeilisearchIndex` gated) |
| `SuggestIndex` | `PostgresSuggestIndex` (`MeilisearchSuggestIndex` gated) — **the gate moved to this port**, which is [ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md) |
| `Embedder` | `FastEmbedEmbedder` — **optional**, behind an extra and off by default; a deployment without it still has full-text and trigram, the tier serving 1.27M titles ([ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)) |
| `LLMClient` | `OpenAICompatibleClient` — **one `POST /v1/chat/completions` over the httpx stack already here, and `litellm` is not taken.** Priced rather than assumed: +146 MB and 29 distributions against +0 and 0, and the 29 are a second async HTTP stack plus two tokenizer runtimes. The provider abstraction is `USHER_LLM_BASE_URL` [ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md) |
| `TitleRepository` | `PostgresTitleRepository` ([ADR-0009](decisions/0009-repositories-are-ports.md)) |
| `Row` | `BaseRow` in `services/rows/base.py` and its **ten** concrete rows — **the base class is in `services/` and the ABC is in `ports/`**, because `hydrate()` reads two repositories off the context and a port with a dependency is not a port ([06](06-rows-and-recommendations.md)) |
| `RowProvider` | `ContinueWatchingProvider`, `NextUpProvider`, `RecentlyAddedProvider`, `RediscoverProvider`, `BecauseYouWatchedProvider`, `FranchiseProvider`, `GenreAffinityProvider`, `SeasonalProvider`, `PeopleProvider`, ✅ `CuratedProvider` — **ten**, registered as `services/rows/__init__.py`'s `ROW_PROVIDERS`. The tenth shipped in M8 with `curated_rows`, `LLMRow` and `RowFamily.CURATED` as one family ([09](09-roadmap.md)'s M7 boundary call 2), and it is the only one that hydrates an artefact a *model* wrote — it reads `curated_rows` through a port on the context and never holds an `LLMClient` |

**`adapters/search/` vs `db/repositories/`.** Both ultimately talk to the
same PostgreSQL instance, which invites conflating them — they are not the
same thing and do not hold the same kind of data. `adapters/search/`
implements the `SearchIndex` and `SuggestIndex` ports (`postgres.py`, with a
gated `meilisearch.py` alongside it): weighted full-text and vector search on
the first, trigram autocomplete on the second
([ADR-0002](decisions/0002-postgres-first-search.md),
[ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)).
Titles, sources, media items, users, and watch state are persisted through
repositories in `db/repositories/`, which implement repository ports
declared in `ports/` (`TitleRepository` —
[ADR-0009](decisions/0009-repositories-are-ports.md)). Repositories are
never adapters — the "db is driven, not driving" import-linter contract and
layering rule 4 above both hold precisely because repositories sit under
`db/`, not `adapters/`.

## Repository layout

```
usher/
├── docs/
│   ├── prd/                    ← this
│   └── specs/                  ← reviewed design specs
├── src/usher/
│   ├── api/         routers/ (health, titles, events, home, rows, sources),
│   │                deps.py, dto/ (… home.py), lanes.py
│   ├── domain/      title.py, person.py, source.py, watch.py, rows.py,
│   │                curation.py
│   ├── ports/       *.py  (ABCs only)
│   ├── adapters/    emby/, tmdb/, bulk/ (… movielens.py), search/,
│   │                embedding/, llm/ (openai_compatible.py)
│   ├── services/    rows/ (base.py, cache.py, one module per provider),
│   │                home.py, taste.py, derive.py, similar.py, search.py,
│   │                matching.py, ingest.py, enrich.py, push.py, jobs.py,
│   │                curation.py, curation_pool.py, curation_prompt.py,
│   │                curation_validate.py, query_expansion.py,
│   │                llm_ledger.py (the one `llm_calls` writer)
│   ├── jobs/        queue.py, scheduler.py, tasks/
│   ├── db/          models/ (… people.py, collection.py, taste.py,
│   │                curation.py),
│   │                repositories/ (implement ports/), migrations/
│   └── config.py
├── tests/           unit/, integration/, fixtures/, fakes/ (port doubles
│                    services are unit-tested against, e.g. FakeTitleRepository)
├── compose.yml
└── pyproject.toml
```

Files stay small and single-purpose. A growing file is a signal that a concept
wants extracting, not that it needs sections.

⏳ **One entry in that tree, and in the diagram at the top of this file, does
not exist as written** — recorded rather than quietly redrawn, because the
tree is what a new reader navigates by. (It read *"Two entries"* until M8; the
second was `adapters/llm/`, struck through below because M8 built it.)

- **There is no `jobs/` package.** The priority queue landed as
  `ports/jobs.py` + `db/repositories/jobs.py` (a repository, per
  [ADR-0009](decisions/0009-repositories-are-ports.md), which is why it is
  under `db/`), the worker as `services/jobs.py`, and the "scheduler" as
  `api/lanes.py`'s supervised lanes. Nothing was skipped; the concepts landed
  under the layers that own them.
- ~~**There is no `adapters/llm/`.**~~ **Built in M8**, and the entry it said
  was "a plan, not an inventory" was also wrong about *what* was planned:
  `LLMClient → LiteLLMClient` became `OpenAICompatibleClient`
  ([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md)). The
  directory keeps its capability name and the *reason* changes — an
  OpenAI-compatible client has no single upstream either, because its upstream
  is whatever `base_url` points at. `adapters/search/` and
  `adapters/embedding/` are real as of M6.

## Stack

| | |
|---|---|
| Language | Python 3.13 |
| Web | FastAPI + uvicorn |
| Models | Pydantic v2 |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| DB | PostgreSQL 17 + pgvector ≥ 0.8.5 |
| Jobs | In-process asyncio workers over a Postgres-backed queue |
| LLM | Any OpenAI-compatible endpoint, over httpx — `USHER_LLM_BASE_URL` ([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md)) |
| Embeddings | fastembed, local, **optional** (167 MiB, no torch) |
| Packaging | uv |
| License | MIT |

Chosen to match Alfred so models and tooling are shared rather than bridged.

## Concurrency model

Everything is asyncio in one process. Work is separated by lane, each with its
own semaphore, so a slow upstream can't starve the API:

| Lane | Concurrency | Bounded by |
|---|---|---|
| API request handling | uvicorn default | — |
| Source event stream | 1 per source | push connection |
| **Job worker, globally** (M9 W1) | **`USHER_JOB_CONCURRENCY`, default 12** | the connection pool; `Settings` refuses a value it cannot serve |
| — `enrich` | the global (12) | TMDb, at `USHER_TMDB_REQUESTS_PER_SECOND` |
| — `match`, `watch_history`, `watch_writeback` | 4 | **measured** (M10 S7): flat latency and 3.89× throughput at 4 in flight |
| — `derive` | 4 | **measured** (M10 S7): the knee — the 8th job buys +13% for +71% latency |
| — `index` | 1 | `fastembed` is CPU-bound at a flat tokens/s ceiling |
| — `curate` | 1 | the reference endpoint has 56 tokens of context spare |
| — `sync`, `bootstrap` | 1 | a walk of the whole library; a session `bulk_load_window` commits |
| Embedding | the `index` row above; `USHER_EMBEDDING_BATCH_SIZE` is its batch | CPU/GPU |
| **Row build** (M7) | **1, sequential — and not a setting** | `AsyncSession` |
| **Screen refresh** (M9) | **1 lane, 1 refresh in flight, ≤ 32 keys queued** | `REFRESH_QUEUE_SIZE`; full means dropped |

✅ **The three rows this table carried as "design, not shipped" are shipped in
M9's W1, and the numbers are different from the ones it guessed.** It used to
read *"there is no semaphore anywhere in `src/`"*, and that was the whole
finding: `JobWorker` claimed a batch of twenty and awaited them **one at a
time**, so enrichment concurrency was 1 rather than 8. M9's S3 measured what
that cost against the live TMDb API — **19.76 rps on three worker processes,
against a token bucket configured at 10 rps per process that was never the
binding constraint on any of them**, with per-worker throughput *rising* from
6.59 to 7.72 rps when one of the three died. The architecture was the ceiling,
not the policy.

What replaced it is a **bounded pool per kind on one worker**, not a lane per
kind: `usher.services.jobs.KIND_CONCURRENCY` is a table over every `JobKind`,
resolved against the global at build time, and **each entry names the
measurement it came from in its own source.** The global default of 12 is
Little's law over S3's measured tail (p95 HTTP 0.4267 s plus ~0.033 s of
per-job bookkeeping, so ~11.5 in flight to hold ADR-0005's ~25 rps), not a
round number.

✅ **The four entries that were bounds are measurements as of 2026-08-19 (M10
S7), and until then this page contradicted itself about how many there were.**
The paragraph here called the `match`/`watch_history`/`watch_writeback` row
*"the one number here that is a bound rather than a measurement"* while the
table three rows up described `derive` as *"the pool's share, not a measured
throughput"* — two claims on one page each naming a different single unmeasured
number, while both in fact flagged both. `usher/services/jobs.py` carried the
mirror image of the same error, and `.claude/rules/tmdb-and-enrichment.md` said
*"two of the eight"* against a table of **nine**. It was **four entries and two
justifications**, and all three documents are corrected together.

🔴 **Nothing pinned those four, either, and that was demonstrated rather than
argued**: `KIND_CONCURRENCY[MATCH]` set to 7 and `[DERIVE]` to 9 passed all
**4,119** unit cases. `tests/unit/test_config.py::test_the_four_concurrency_
entries_that_are_bounds_are_pinned_by_value_and_say_which_measurement_moved_
them` now pins each by literal.

**What S7 measured, and the second half is the one that reframes the first:**

- **The three Emby-facing kinds do not degrade at 4.** 44 bounded read-only
  requests, `get_item` at 1, 2 and 4 in flight with the gate off: median
  **0.1377 / 0.1405 / 0.1363 s** — flat, the c=4 median 1% *below* c=1 — and
  steady-state **7.40 / 14.21 / 28.75 rps**, i.e. **3.89×** at four in flight.
  The W1-shaped prediction that a household server would show TMDb's 37%
  per-worker loss is **refuted** for this workload.
- ⚠️ **But 4 is a slot count, not a request rate, and has been since S3.** With
  `USHER_SOURCE_REQUESTS_PER_SECOND` at its shipped **0.4**, four coroutines
  against one source were measured issuing requests **2.50 s apart, peak one in
  flight** — `_MinInterval` holds its lock across the wait and one source has
  one gate. The concurrency entry bounds jobs in flight, and therefore sessions
  and connections; the **gate** bounds the wire. Raising this row would not
  raise the request rate.
- **`derive`'s knee is at 4.** 200 jobs a rung on one pool: **48.7 / 85.3 /
  115.7 / 130.7 jobs/s** at 1 / 2 / 4 / 8, per-job median **19.8 / 22.6 / 31.8 /
  54.2 ms**. The eighth in-flight job buys +13% throughput for +71% latency, so
  the pool-budget argument and the throughput measurement agree.

**What is still unverified is named rather than implied**: any Emby build but
4.9.5.0, this server under a *paging* load rather than single-item reads (a
page is ~34× dearer, see below), and N > 1 Usher processes against one source —
which no limiter here can express, because every one of them is per process.

🔴 **That string has now been measured, and it was never about a request.**
M10's S1 (2026-08-15, 52 bounded read-only requests against the operator's
live Emby — full table and method in `.claude/rules/emby-push-and-ingest.md`)
prices this deployment's source at:

| op | median | mean | p95 | max | n |
|---|---|---|---|---|---|
| `verify` — `GET /System/Info/Public` | 0.1253 s | 0.1543 s | 0.4721 s | 0.4721 s | 12 |
| `get_item` — one item, full `Fields` | 0.1495 s | 0.1649 s | 0.3587 s | 0.3587 s | 12 |
| `list` — a 200-item page | 5.0954 s | 6.0369 s | 9.1713 s | 9.6805 s | 24 |

One household, one evening, one Emby build, **sequential** — not a constant.
Three consequences for the numbers on this page:

- **A single-item read is ~34× cheaper than a page.** The three job kinds
  capped at 4 all make single-item reads, so the upstream they are capped
  against runs at ~6 rps from one coroutine, not at one request per 1–5 s.
- **A page is dearer than the old string, not cheaper.** A full walk of the
  1,134,919-item library is 5,675 pages, i.e. **7.3–11.8 h** against the
  1.6–7.8 h the old figure implied.
- **The old figure was never a measurement.** It entered the repository in
  `0c823e0` on 2026-07-28, the first PRD commit, two days before an Emby
  adapter existed; it was cited **22** times and called *measured* 11 times, and
  the paragraph above used to attribute it to "the old table" — i.e. to an
  earlier revision of this document.

**Every job in flight holds an `AsyncSession`**, which is why
`USHER_DB_POOL_SIZE` exists and why `Settings` refuses a `job_concurrency` the
pool cannot serve: over capacity SQLAlchemy's `QueuePool` does not fail fast,
it waits `pool_timeout` per checkout and then raises, so the symptom is a lane
getting slower until it starts parking jobs.

✅ **M8 adds a sixth thing to that same one worker and no row to this table,
which is a decision rather than an omission.** `JobKind.CURATE` is registered
on the same `JobWorker`, guarded on an `LLMClient` existing exactly as `INDEX`
is guarded on an embedder — so a deployment with `USHER_LLM_ENABLED=false`
(the default) never claims one. The operational consequence worth stating is
that a generation holds the single worker for as long as the completion takes,
up to `USHER_LLM_TIMEOUT_SECONDS` (120 s), while the other five kinds —
`match`, `watch_history`, `enrich`, `index` and `derive` — wait behind it.
`watch_history` is worth naming rather than eliding: with `match` it is one of
only two kinds *every* deployment registers, so it is the one waiting on the
deployment that has nothing else configured. That is acceptable at the shape this runs in — PRD 06
budgets *one* completion per household per day, against a queue whose other
kinds are minutes of background work — and it is the number to look at first if
a queue ever appears to stall on a curating deployment. A lane of its own is
the fix if it stops being acceptable, not a semaphore: the ceiling here is one
upstream call, not concurrency.

✅ **M9's screen-refresh row is a real lane with a real number, and it is here
because [06](06-rows-and-recommendations.md)'s "served stale while refreshing"
is otherwise a background task nobody can put a ceiling on.** One
`asyncio.Task` in `api/lanes.py` drains a bounded deduplicating queue of stale
screen keys one at a time, so the pool sees **at most one extra session** on
top of the push lanes and the worker; the queue holds 32 keys and `schedule`
**drops** rather than blocks when it is full, because a request path that
awaited a full queue would block on exactly the load that filled it. Dropping
costs one hard miss on the next request past the grace window — the cost M7
paid on every screen expiry — which is what makes drop-on-full the safe choice
rather than merely the convenient one. It is gated on `create_app` building a
cache and a queue rather than on a setting: a switch here would configure the
one state serve-stale must never reach, a stale screen with nothing behind it.
Not a source lane, so `/health/ready`'s `lanes.push` and its status code are
unchanged by it.

**The row-build row is the one line in this table that is a decision rather
than a design**, and it is here because a concurrency table that silently omits
the one loop a reader would expect to find in it is how somebody adds
`asyncio.gather` in good faith. `HomeService` builds the selected rows in a
`for`, on the request's own session, and 1 is the *correct* number rather than
an unraised limit: `AsyncSession` is explicitly not safe for concurrent use, so
ten coroutines awaiting on one session interleave on one connection — a
corruption that usually works, failing as an intermittent `InvalidRequestError`
under load. The two escapes are worse at this scale (a session per row is ten
connections for one home screen; a semaphore has no lane to belong to, which is
this very table's gap). There is **no setting**, because
[08](08-operations.md) already retracted "concurrency per lane" on the
principle that a setting cannot be added ahead of the mechanism it would bound,
and the mechanism here is a `for`. Measured rather than assumed: p50 23.9 ms,
p95 35.9 ms cold over nine providers on a real 1,271,570-title catalog — ⚠️
**M7's registry and M7's household, not re-run for the tenth**; M8 added
`CuratedProvider`, whose propose is one indexed read of `curated_rows` and
whose build hydrates stored ids, so it is the *cheapest* of the ten and the
measurement is expected to move by less than its own noise. Expected, not
measured, and marked so.
[ADR-0025](decisions/0025-rows-build-sequentially.md).

The row that is worth revisiting rather than merely correcting is the
embedding one: embedding is CPU-bound work sharing a worker with two I/O-bound job
kinds, so a long backfill delays every `match` behind it. M6 bounds the damage
by enqueueing `index` at `BACKFILL` priority, which is a priority answer to a
scheduling question and is enough at 2k–10k titles.

A `--worker` entrypoint flag exists from day one so lanes can be moved to a
separate container later by editing compose, with no code change.

## Extension seams left open in v1

Deliberately designed-for but not built:

- **Authentication.** All routes take a `current_user` dependency that returns
  the singleton default user in v1. Adding real auth replaces one dependency;
  watch state and taste are already per-user.
- **Additional sources.** `MediaItem` is many-per-title from the start.
- **Additional metadata providers.** Provider precedence is a config list; field
  provenance is recorded per title.
- **Alternative search backends.** `SearchIndex` and `SuggestIndex` are ports
  with a measurable swap criterion, and the criterion applies to the second
  one: the swap ADR-0002 contemplates is the instant-search box, which is
  what put `suggest` on its own port
  ([ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)).
