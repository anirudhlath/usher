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
│               Row · RowProvider          (all ABCs)       │
│  adapters/    emby/ · tmdb/ · imdb_dumps/ · postgres/ ·   │
│               sentence_transformers/ · litellm/           │
│  jobs/        priority queue · schedulers · workers       │
│  db/          SQLAlchemy 2.0 async · Alembic · repos      │
└────────────────────────────┬──────────────────────────────┘
                             ▼
                       [PostgreSQL]
             canonical catalog · search · vectors · job queue
```

**Deployment:** `compose.yml` with `usher` + `postgres`. One stateful service.
An optional `meilisearch` service exists behind a feature gate — see
[ADR-0002](decisions/0002-postgres-first-search.md).

## Layering rules

These are the invariants that keep Emby out of everything:

1. **`domain/` imports nothing from `adapters/`, `db/`, or `api/`.** It is pure
   Pydantic models and value objects.
2. **`services/` depends only on `domain/` and `ports/`.** A service never
   imports an adapter; it receives one.
3. **`adapters/` implement `ports/` and may import `domain/`.** They translate
   foreign shapes into canonical ones at the boundary. Raw Emby or TMDb JSON
   never escapes its adapter package.
4. **`db/` models are separate from `domain/` models.** Repositories translate.
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

    @abstractmethod
    async def list_items(self, since: datetime | None) -> AsyncIterator[SourceItem]: ...
    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None: ...
    @abstractmethod
    async def stream_url(self, external_id: str) -> StreamTarget: ...
    @abstractmethod
    async def watch_state(self, since: datetime | None) -> AsyncIterator[SourceWatchState]: ...
    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None: ...
    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise NotSupported; the
        reconciler covers them."""
```

Other ports follow the same pattern:

| Port | Implementations (v1) |
|---|---|
| `SourceAdapter` | `EmbyAdapter` |
| `MetadataProvider` | `TMDbProvider` |
| `BulkDataset` | `IMDbDumps`, `TMDbIdExport`, `WikidataCrosswalk`, `MovieLensGenome` |
| `SearchIndex` | `PostgresSearchIndex` (`MeilisearchIndex` gated) |
| `Embedder` | `SentenceTransformerEmbedder` |
| `LLMClient` | `LiteLLMClient` |
| `Row` / `RowProvider` | see [06](06-rows-and-recommendations.md) |

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
│   ├── db/          models/, repositories/, migrations/
│   └── config.py
├── tests/           unit/, integration/, fixtures/
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
| Embeddings | sentence-transformers, local |
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
- **Alternative search backends.** `SearchIndex` is a port with a measurable
  swap criterion.
