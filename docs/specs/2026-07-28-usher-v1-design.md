# Usher v1 — Design Spec

**Date:** 2026-07-28
**Status:** Awaiting review
**PRD:** [`docs/prd/`](../prd/README.md) — authoritative for *what and why*.
This spec is the point-in-time design for v1, scoped for an implementation plan.

## Goal

A self-hosted, MIT-licensed media catalog backend that fully abstracts media
servers behind its own canonical database, and exposes an API rich enough to
build an Infuse-class client against — including search, similarity, and
dynamically composed recommendation rows.

The concrete v1 success condition: **a client built against Usher can replace
direct Emby access entirely, for both films and television.**

## Scope

### In

- Canonical catalog for **movies and TV** (series → season → episode).
- **Emby** as the first source, behind a `SourceAdapter` abstraction.
- **TMDb** as the sole metadata provider, behind a `MetadataProvider`
  abstraction.
- **Bulk bootstrap** from IMDb dumps, TMDb ID exports, Wikidata crosswalk, and
  MovieLens tag genome.
- **Postgres-backed search**: full-text, typo-tolerant autocomplete, vector
  similarity, fused with RRF.
- **Rows**: system, similarity, and LLM-curated, composed dynamically per
  request.
- **Unified watch state**, canonical in Usher, synced bidirectionally with Emby.
- **REST + OpenAPI** surface with an SSE update channel and an image proxy.
- **Telemetry** — loguru + OpenTelemetry, and five Grafana dashboards shipped as
  provisioned JSON.
- Single-host Docker deployment.

### Out

- Authentication (seam left; see PRD [01](../prd/01-architecture.md)).
- Any UI or client.
- Transcoding, streaming, file management.
- Additional sources or metadata providers.
- Multi-tenancy, commercial use, collaborative filtering.

## Architecture

Modular monolith, hexagonal core. One FastAPI app, one Postgres. Full detail in
PRD [01](../prd/01-architecture.md).

```
api/  →  services/  →  ports/ (ABCs)  ←  adapters/
              ↓
         domain/ (Pydantic)      db/ (SQLAlchemy + repos)
```

Layering is enforced in CI with `import-linter`, because documented-only rules
become suggestions.

**Stack:** Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 async · Alembic
· PostgreSQL 17 + pgvector ≥ 0.8.5 · litellm · sentence-transformers · uv.

**Ports (all `abc.ABC`):** `SourceAdapter`, `MetadataProvider`, `BulkDataset`,
`SearchIndex`, `Embedder`, `LLMClient`, `Row`, `RowProvider`.

## Key design decisions

Each has an ADR carrying the reasoning and evidence:

| Decision | ADR |
|---|---|
| ABCs rather than Protocols for all ports | [0001](../prd/decisions/0001-abc-over-protocol.md) |
| Postgres-first search; Meilisearch behind a measurable gate | [0002](../prd/decisions/0002-postgres-first-search.md) |
| Usher-owned UUIDv7 identity; provider IDs as attributes | [0003](../prd/decisions/0003-own-uuid-identity.md) |
| Push events primary, reconciliation as backstop | [0004](../prd/decisions/0004-push-over-polling.md) |
| Pre-build the catalog from bulk datasets | [0005](../prd/decisions/0005-bulk-bootstrap.md) |
| Server composes the home screen; REST + OpenAPI | [0006](../prd/decisions/0006-server-composed-home.md) |
| Three telemetry datasources; external shared LGTM stack | [0007](../prd/decisions/0007-telemetry-architecture.md) |

## Data model

Full entity definitions in PRD [02](../prd/02-data-model.md). The shape that
matters most:

```
Title (canonical, UUIDv7)
  ├── tmdb_id / imdb_id / tvdb_id      indexed attributes, never identity
  ├── enrichment_state                 skeleton | stub | enriched | failed
  ├── Season → Episode                 series hierarchy
  ├── Credit → Person                  canonical people
  ├── Image                            references; proxied and cached lazily
  ├── MediaItem → Source               "watchable here" (1..n, the extension point)
  ├── WatchState → User                attached to the title, not the source
  └── TitleEmbedding                   halfvec(384)
```

Two invariants do most of the work:

1. **Watch state attaches to the canonical Title**, so it survives adding,
   changing, or losing a source.
2. **A Title with no MediaItem is legitimate** — that is most of the catalog
   after bootstrap, and it is what makes recommending un-owned titles possible.

## Critical flows

### Cold start

```
bootstrap (IMDb → TMDb IDs → Wikidata → TMDb crawl → genome → embed)
    ~3–5 h unattended, resumable, browsable throughout
```

### Source ingest

```
Emby item ──▶ ingest ──▶ match ──▶ enrich ──▶ index
              (stub      (local,   (TMDb)    (tsvector
               visible    offline)            + embedding)
               instantly)
```

Match resolves locally against the bootstrapped skeleton — provider ID, then
IMDb ID, then name+year above a confidence bar — falling back to the TMDb search
API only as a last resort. No confident match → review queue, never dropped.

### Read-through

```
client opens stub ──▶ stub returned immediately (enrichment_state: "stub")
                 └──▶ job promoted to priority 100
                          └──▶ enriched ──▶ indexed ──▶ SSE title.updated
                                                          └──▶ client patches
```

Target: < 5 s from open to enriched.

### Home screen

```
RowProviders propose scored rows ──▶ rank + diversity constraints
    ──▶ build top N concurrently ──▶ drop empties ──▶ ordered BuiltRows
```

## Build sequence

| # | Milestone | Delivers |
|---|---|---|
| M1 | Foundation | Repo, uv, compose, Postgres, migrations, domain models, port ABCs, config, health, CI with layering checks, **telemetry bootstrap** (loguru + OTel, trace context in logs) |
| M2 | Bootstrap | IMDb skeleton, TMDb ID export, Wikidata crosswalk; resumable, checkpointed importers |
| M3 | Emby adapter | Durable-client auth, listing, watch state, stream targets; **contract test suite** |
| M4 | Ingest pipeline | ingest → match → enrich → index; priority queue; stub-on-sight; review queue |
| M5 | Push + read-through | WebSocket events, reconnect/reconcile, demand promotion, SSE |
| M6 | Search | Full-text, autocomplete path, embeddings, RRF, similarity, neighbour precompute |
| M7 | Rows | `Row`/`RowProvider` hierarchy, system + similarity rows, taste centroid |
| M8 | Curation | LLM row generation, validation, persistence |
| M9 | API surface | Full REST surface, image proxy, playback resolution, attribution |
| M10 | Hardening | Grafana dashboards, alerts, failure modes, backup/restore, docs, public release |

Each milestone is independently testable. M1–M4 produce a browsable catalog;
M5–M6 make it fast; M7–M9 make it a product.

**Instrumentation is cross-cutting, not a milestone.** M1 lands the telemetry
bootstrap; every subsequent milestone instruments its own work as it is built
(spans on the pipeline in M4, push metrics in M5, search metrics and the
`search_queries` table in M6, `llm_calls` in M8). M10 assembles the dashboards
over data that is already flowing, rather than retrofitting instrumentation at
the end.

## Acceptance criteria

| Criterion | Target |
|---|---|
| Home screen, warm | < 150 ms fully composed |
| Search-as-you-type | < 50 ms |
| Title detail, enriched | < 100 ms |
| Catalog browsable after source connect | Seconds, not after sync completes |
| Open → enriched → pushed | < 5 s |
| Watch state round-trip (Emby → Usher) | < 10 s with push |
| Source abstraction | Zero source-specific concepts outside `adapters/emby/` |
| Adapter contract suite | Passes for Emby; written to be source-agnostic |
| Bootstrap | Resumable; survives restart mid-import |

## Testing

- **Unit** — services against port fakes; no network.
- **Integration** — real Postgres via testcontainers; recorded provider payloads
  as committed fixtures.
- **Contract** — one parametrised suite every `SourceAdapter` must pass. This is
  the test that proves the abstraction is real.
- **Bootstrap** — small committed dataset slices; never a full download in CI.

TDD throughout: failing test first.

## Risks and open items

| Risk | Impact | Mitigation |
|---|---|---|
| **Emby WebSocket returned 404 on probe** | Push path unavailable; sync falls back to polling | **Resolve first in M3.** Retest with a live token and a real handshake. Fallbacks: per-user Emby webhooks (needs the host to enable Notifications under Feature Access + Premiere active), then polling. Adapter reports `supports_push`; nothing above it changes. |
| Postgres typo tolerance below bar | Search UX worse than Meilisearch on short titles | Measurable gate in PRD [05](../prd/05-search-and-similarity.md); `SearchIndex` port makes the swap contained |
| TMDb crawl slower than estimated | Longer bootstrap | Tiered — Tier 1 (189k) is sufficient; catalog is usable throughout |
| LLM row quality poor | Weak curated rows | Validated against catalog IDs; rows are additive, system rows carry the screen |
| Match false positives | Wrong metadata on a title | Confidence bar + review queue + admin override; provenance recorded |
| Emby token expiry | The original failure mode | Stable `DeviceId` + credential re-auth on 401 |

**Immediate first action:** re-mint a working Emby token and retest
`/embywebsocket` with a proper handshake. It settles the M5 design and, as a
side effect, fixes the currently-broken Home Assistant movie tab.

## Licensing

MIT code. **Ship importers, never data** — no third-party metadata in the
repository or release artifacts. Each user runs importers and holds their own
TMDb key. IMDb and TMDb attribution strings are served from `/meta/attribution`
for clients to display. Commercial use is explicitly out of scope; both
providers require separate licensing for it.
