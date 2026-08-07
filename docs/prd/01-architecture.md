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
│   │                curation_validate.py, query_expansion.py
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
| Enrichment workers | 8 | TMDb rate limit (~40 rps ceiling) |
| Source sync workers | 4 | Emby is slow (~1–5 s/request observed) |
| Embedding | 1 batch worker | CPU/GPU |
| **Row build** (M7) | **1, sequential — and not a setting** | `AsyncSession` |

⏳ **This table is the design, and three of its rows are not what shipped** —
enrichment workers, source sync workers and embedding. (The row-build row below
them is the exception and is described rather than corrected; see the paragraph
after this one.) There is **no semaphore anywhere in `src/`** and none of those
three numbers exists as a limit. What actually bounds the work: one `JobWorker` claiming a
batch and running it **sequentially**, so enrichment concurrency is 1, not 8;
TMDb is bounded by a **token bucket** at `USHER_TMDB_REQUESTS_PER_SECOND`
rather than by a worker count, which is the more direct control over the thing
the row names; source sync is one sequential walk per push lane; and
**embedding has no lane of its own at all** — `JobKind.INDEX` is registered on
the same `JobWorker` as `match` and `enrich`, so its "1 batch worker" is
really "whatever the one worker is doing next".
`USHER_EMBEDDING_BATCH_SIZE` is the embedder's internal batch, not a lane.

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
