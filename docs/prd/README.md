# Usher — Product Requirements

Usher is a self-hosted media catalog backend: a canonical database of film and
television, assembled from bulk open datasets and enriched on demand, with
pluggable *sources* (Emby first) telling it where each title can actually be
watched.

This directory is the **living PRD**. It describes what Usher does, feature by
feature, in user-facing terms.

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

**The design spec is a separate artifact.** The PRD says what Usher does; a
spec is the point-in-time, reviewed design handed to an implementation plan.
When the two disagree, the PRD is authoritative and the spec is stale.

Current spec: [`docs/specs/2026-07-28-usher-v1-design.md`](../specs/2026-07-28-usher-v1-design.md)

## Implementation plans

Point-in-time task breakdowns handed to implementation. Like specs, these are
historical once executed; the PRD above stays authoritative when they disagree.

The second column is headed *Scope* rather than *Milestone* because not every
plan is a milestone — a quality-eval phase and two one-off fixes are not.

| Plan | Scope | Status |
|---|---|---|
| [2026-07-28-m1-foundation.md](../plans/2026-07-28-m1-foundation.md) | M1 — Foundation | ✅ complete |
| [2026-07-30-m2-bootstrap.md](../plans/2026-07-30-m2-bootstrap.md) | M2 — Catalog bootstrap (PRD [04](04-catalog-bootstrap.md) Phases 0–2) | ✅ complete |
| [2026-07-30-m3-emby-adapter.md](../plans/2026-07-30-m3-emby-adapter.md) | M3 — Emby adapter (PRD [03](03-sources-and-sync.md)) | ✅ complete |
| [2026-07-31-m4-ingest.md](../plans/2026-07-31-m4-ingest.md) | M4 — Ingest pipeline (PRD [03](03-sources-and-sync.md) stages 1–3) | ✅ complete |
| [2026-08-01-m5-push.md](../plans/2026-08-01-m5-push.md) | M5 — Push and read-through (PRD [03](03-sources-and-sync.md) push lane, [07](07-client-api.md) `GET /titles/{id}` and `GET /events`) | ✅ complete |
| [2026-08-02-m6-search.md](../plans/2026-08-02-m6-search.md) | M6 — Search (PRD [03](03-sources-and-sync.md) stage 4 and all of [05](05-search-and-similarity.md)) | ✅ complete — its Meilisearch gate ran and failed; the two-tier suggest it obliged shipped in M9 |
| [2026-08-03-m7-rows.md](../plans/2026-08-03-m7-rows.md) | M7 — Rows (all of [06](06-rows-and-recommendations.md) but LLM curation, plus [07](07-client-api.md)'s `GET /home`) | ✅ complete |
| [2026-08-06-m8-curation.md](../plans/2026-08-06-m8-curation.md) | M8 — Curation ([06](06-rows-and-recommendations.md)'s *LLM curation* section, [10](10-telemetry-and-dashboards.md)'s `llm_calls`, M6's query expansion, the genome's tag vocabulary) | ✅ complete — two of its own claims were refuted by its live run: query expansion measured *worse*, and most generated headings were the genre labels the prompt forbids |
| [2026-08-10-m9-api-surface.md](../plans/2026-08-10-m9-api-surface.md) | M9 — API surface (all four of [07](07-client-api.md)'s endpoint tables, [10](10-telemetry-and-dashboards.md)'s `search_queries`, the IMDb bulk expansion in [04](04-catalog-bootstrap.md)) | ✅ complete — two of its own measurements came back as refusals and both shipped as such: the IMDb entity design failed its size bar and was withdrawn, and the tag-genome similarity term was removed on a measured coverage floor |
| [2026-08-13-m10-hardening.md](../plans/2026-08-13-m10-hardening.md) | M10 — Hardening ([08](08-operations.md)'s failure, backup and secrets sections, all of [10](10-telemetry-and-dashboards.md)'s dashboards and alerts, and the public release) | ✅ complete — all 67 tasks, merged by PR #44 |
| [2026-08-18-e1-eval-skeleton-and-suggest.md](../plans/2026-08-18-e1-eval-skeleton-and-suggest.md) | E1 — quality evals, phase 1 of 4 (**not a milestone**): the `usher.eval` skeleton and the suggest surface | ✅ merged — ⏳ three `fuzzy recall_at_5` bars stay open, because filling them from an unrepaired system would pin a bar its fix fails |
| [2026-08-19-rating-provenance-split.md](../plans/2026-08-19-rating-provenance-split.md) | Rating provenance (**neither a milestone nor an eval phase**): the three `titles` columns written by two sources each | ✅ complete — each rating column now names its source, and the rollback table `titles_rating_backup_20260819` must not be dropped without an operator's say-so |
| [2026-08-21-issue-41-resumable-watch-lane.md](../plans/2026-08-21-issue-41-resumable-watch-lane.md) | Resumable watch lane (**neither a milestone nor an eval phase**): the watch-state walk checkpointed on its `StartIndex` so a transient failure costs one page rather than the whole walk | ✅ complete — ⏳ the walk itself has still never been run to completion; that is the operator's step and nothing schedules one |
| [2026-09-12-comment-convention-and-polish.md](../plans/2026-09-12-comment-convention-and-polish.md) | M10 polish (**not a milestone of its own**): the comment convention, the `/simplify` findings, and the prose cut across code and the PRD | ✅ complete — all four stages, merged with M10 by PR #44 |
| [2026-09-12-stage-1a-shared-infrastructure.md](../plans/2026-09-12-stage-1a-shared-infrastructure.md) | M10 polish, stage 1A: the reader slot, the narrow scope, the non-finite refusal | ✅ complete — merged with M10 by PR #44 |

## Conventions for maintaining this

- **One concern per file.** If a file starts covering two subsystems, split it.
- **State the behaviour, not the reasoning.** A PRD section says what Usher
  does; the argument for it belongs with the code or the plan that made it.
- **Mark uncertainty explicitly.** Use ⏳ for not-yet-designed and 🔶 for
  provisional. Never leave silent placeholders — an unmarked section reads as
  settled when it isn't.
- **A stated fact that stops being true is corrected, not left standing.**
