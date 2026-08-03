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
│               search · rows · watchstate · bootstrap      │
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
names and no upstream service at all — and `litellm` is itself a
multi-provider abstraction, not one upstream. (This example read
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
([ADR-0002](decisions/0002-postgres-first-search.md)) and has **⏳ not yet been
run**. If it is ever taken, the service is added behind the `SuggestIndex`
port alone — that being the whole of
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
layering rules that are only documented become suggestions.

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
| `BulkDataset` | `IMDbDumps`, `TMDbIdExport`, `WikidataCrosswalk` — ⏳ `MovieLensGenome` **does not exist**; owned by M7, see [09](09-roadmap.md) |
| `SearchIndex` | `PostgresSearchIndex` (`MeilisearchIndex` gated) |
| `SuggestIndex` | `PostgresSuggestIndex` (`MeilisearchSuggestIndex` gated) — **the gate moved to this port**, which is [ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md) |
| `Embedder` | `FastEmbedEmbedder` — **optional**, behind an extra and off by default; a deployment without it still has full-text and trigram, the tier serving 1.27M titles ([ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)) |
| `LLMClient` | `LiteLLMClient` |
| `TitleRepository` | `PostgresTitleRepository` ([ADR-0009](decisions/0009-repositories-are-ports.md)) |
| `Row` / `RowProvider` | see [06](06-rows-and-recommendations.md) |

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
│   ├── api/         routers/, deps.py, dto/
│   ├── domain/      title.py, person.py, source.py, watch.py, rows.py
│   ├── ports/       *.py  (ABCs only)
│   ├── adapters/    emby/, tmdb/, bulk/, search/, embedding/, llm/
│   ├── services/
│   ├── jobs/        queue.py, scheduler.py, tasks/
│   ├── db/          models/, repositories/ (implement ports/), migrations/
│   └── config.py
├── tests/           unit/, integration/, fixtures/, fakes/ (port doubles
│                    services are unit-tested against, e.g. FakeTitleRepository)
├── compose.yml
└── pyproject.toml
```

Files stay small and single-purpose. A growing file is a signal that a concept
wants extracting, not that it needs sections.

## Stack

| | |
|---|---|
| Language | Python 3.13 |
| Web | FastAPI + uvicorn |
| Models | Pydantic v2 |
| ORM | SQLAlchemy 2.0 (async) + Alembic |
| DB | PostgreSQL 17 + pgvector ≥ 0.8.5 |
| Jobs | In-process asyncio workers over a Postgres-backed queue |
| LLM | litellm (provider-agnostic) |
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
