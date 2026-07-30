# 09 — Roadmap

> 🔶 **Provisional.** Sequencing is sound but will be refined once
> [07](07-client-api.md) and [08](08-operations.md) are brainstormed and the
> implementation plan is written.

## v1 — the abstraction works end to end

Success condition: a client can be built against Usher that fully replaces
direct Emby access, for both movies and television.

| Milestone | Contents |
|---|---|
| **M1 — Foundation** ✅ | Repo, uv project, compose, Postgres + migrations, domain models, port ABCs, config, health, CI with layering checks, telemetry bootstrap |
| **M2 — Bootstrap** ✅ | IMDb skeleton, TMDb ID export, Wikidata crosswalk; resumable importers |
| **M3 — Emby adapter** | Durable-client auth, item listing, watch-state read/write, stream targets; adapter contract tests |
| **M4 — Ingest pipeline** | Ingest → match → enrich → index; priority queue; stub-on-sight; unmatched review |
| **M5 — Push and read-through** | WebSocket events, reconnect/reconcile, demand promotion, SSE to clients |
| **M6 — Search** | Full-text, autocomplete path, embeddings, RRF fusion, similarity, neighbour precompute |
| **M7 — Rows** | Row and RowProvider hierarchy, system rows, similarity rows, taste centroid |
| **M8 — Curation** | LLM row generation, validation, persistence, regeneration job |
| **M9 — API surface** | Full HTTP surface, image proxy, playback resolution, attribution |
| **M10 — Hardening** | Observability, failure modes, backup/restore, docs, public release |

TV is in scope throughout, not deferred — series/season/episode modelling,
Next Up, and episode-level watch state land with the milestones that own them.

## Post-v1 candidates

Not committed; recorded so the design keeps room for them.

- **Authentication** — real user accounts and per-client tokens through the seam
  left in [01](01-architecture.md).
- **Additional sources** — Jellyfin and Plex adapters. The genuine test of the
  abstraction is whether these require no change outside their packages.
- **Additional metadata providers** — OMDb for aggregated ratings, TVDb for
  alternate episode orderings, resolved through `field_provenance`.
- **Alfred integration** — media intents, spoken row reasons, voice-driven
  playback.
- **Meilisearch** — only if the typo-tolerance gate in
  [05](05-search-and-similarity.md) fails.
- **Reference client** — separate repository.
- **Request/wanted list** — titles in the catalog but on no source.

## Explicitly out of scope

Transcoding, file management, downloading or acquisition, multi-tenant hosting,
commercial use, and collaborative filtering.
