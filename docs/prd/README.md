# Usher — Product Requirements

Usher is a self-hosted media catalog backend: a canonical database of film and
television, assembled from bulk open datasets and enriched on demand, with
pluggable *sources* (Emby first) telling it where each title can actually be
watched.

This directory is the **living PRD**. It describes what Usher is and why it is
shaped the way it is. It is maintained incrementally — each section is
brainstormed, then written here before implementation begins.

## What lives where

| Document | Purpose | Status |
|---|---|---|
| [00-overview.md](00-overview.md) | Vision, goals, non-goals, success criteria | ✅ agreed |
| [01-architecture.md](01-architecture.md) | System shape, layering, ports & adapters | ✅ agreed |
| [02-data-model.md](02-data-model.md) | Canonical entities, identity, enrichment tiers | ✅ agreed |
| [03-sources-and-sync.md](03-sources-and-sync.md) | Source adapters, push events, priority queue | ✅ agreed |
| [04-catalog-bootstrap.md](04-catalog-bootstrap.md) | Bulk dataset import, licensing rules | ✅ agreed |
| [05-search-and-similarity.md](05-search-and-similarity.md) | Search, embeddings, similarity | ✅ agreed |
| [06-rows-and-recommendations.md](06-rows-and-recommendations.md) | Row hierarchy, LLM curation | ✅ agreed |
| [07-client-api.md](07-client-api.md) | HTTP surface, streaming updates, playback | ✅ agreed |
| [08-operations.md](08-operations.md) | Config, errors, testing, deployment | ✅ agreed |
| [09-roadmap.md](09-roadmap.md) | Phasing and milestones | 🔶 provisional |
| [10-telemetry-and-dashboards.md](10-telemetry-and-dashboards.md) | Instrumentation, metrics, Grafana dashboards | ✅ agreed |
| [decisions/](decisions/) | Architecture decision records | ongoing |

**The design spec is a separate artifact.** The PRD says *what and why*; the spec
is the point-in-time, reviewed design handed to an implementation plan. When the
two disagree, the PRD is authoritative and the spec is stale.

Current spec: [`docs/specs/2026-07-28-usher-v1-design.md`](../specs/2026-07-28-usher-v1-design.md)

## Implementation plans

Point-in-time task breakdowns handed to implementation. Like specs, these are
historical once executed; the PRD above stays authoritative when they disagree.

| Plan | Milestone | Status |
|---|---|---|
| [2026-07-28-m1-foundation.md](../plans/2026-07-28-m1-foundation.md) | M1 — Foundation | ✅ complete |
| [2026-07-30-m2-bootstrap.md](../plans/2026-07-30-m2-bootstrap.md) | M2 — Catalog bootstrap (PRD [04](04-catalog-bootstrap.md) Phases 0–2) | ✅ complete |
| [2026-07-30-m3-emby-adapter.md](../plans/2026-07-30-m3-emby-adapter.md) | M3 — Emby adapter (PRD [03](03-sources-and-sync.md)) | ✅ complete |
| [2026-07-31-m4-ingest.md](../plans/2026-07-31-m4-ingest.md) | M4 — Ingest pipeline (PRD [03](03-sources-and-sync.md) stages 1–3) | ✅ complete |
| [2026-08-01-m5-push.md](../plans/2026-08-01-m5-push.md) | M5 — Push and read-through (PRD [03](03-sources-and-sync.md) push lane, [07](07-client-api.md) `GET /titles/{id}` and `GET /events`) | ✅ complete |

## Conventions for maintaining this

- **One concern per file.** If a file starts covering two subsystems, split it.
- **Decisions with a "why" go in `decisions/`**, not inline. PRD sections state
  the outcome and link to the ADR for the reasoning.
- **Mark uncertainty explicitly.** Use ⏳ for not-yet-brainstormed and 🔶 for
  provisional. Never leave silent placeholders — an unmarked section reads as
  settled when it isn't.
- **Verified facts carry a source.** Dataset sizes, rate limits, and API
  behaviours in this PRD were measured or read from primary docs. Keep the
  citation when you copy the number.
