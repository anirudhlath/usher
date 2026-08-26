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

The second column was headed *Milestone* until 2026-08-18 and named one all the
way down, which stopped being true the moment a plan arrived that is **not** a
milestone: E1 is phase 1 of 4 of the quality-eval harness, from
[`docs/specs/2026-08-18-usher-quality-evals-design.md`](../specs/2026-08-18-usher-quality-evals-design.md),
and there is no M-number it could be given. It is headed *Scope* so that the row
can say what a plan covers without asserting which numbering it belongs to.

| Plan | Scope | Status |
|---|---|---|
| [2026-07-28-m1-foundation.md](../plans/2026-07-28-m1-foundation.md) | M1 — Foundation | ✅ complete |
| [2026-07-30-m2-bootstrap.md](../plans/2026-07-30-m2-bootstrap.md) | M2 — Catalog bootstrap (PRD [04](04-catalog-bootstrap.md) Phases 0–2) | ✅ complete |
| [2026-07-30-m3-emby-adapter.md](../plans/2026-07-30-m3-emby-adapter.md) | M3 — Emby adapter (PRD [03](03-sources-and-sync.md)) | ✅ complete |
| [2026-07-31-m4-ingest.md](../plans/2026-07-31-m4-ingest.md) | M4 — Ingest pipeline (PRD [03](03-sources-and-sync.md) stages 1–3) | ✅ complete |
| [2026-08-01-m5-push.md](../plans/2026-08-01-m5-push.md) | M5 — Push and read-through (PRD [03](03-sources-and-sync.md) push lane, [07](07-client-api.md) `GET /titles/{id}` and `GET /events`) | ✅ complete |
| [2026-08-02-m6-search.md](../plans/2026-08-02-m6-search.md) | M6 — Search (PRD [03](03-sources-and-sync.md) stage 4 and all of [05](05-search-and-similarity.md)) | ✅ complete, gate included — [ADR-0002](decisions/0002-postgres-first-search.md)'s gate ran 2026-08-03 and **failed**; ✅ the follow-up it obliged — the two-tier suggest — **shipped in M9 on 2026-08-12** ([ADR-0031](decisions/0031-the-two-tier-suggest.md)) |
| [2026-08-03-m7-rows.md](../plans/2026-08-03-m7-rows.md) | M7 — Rows (all of [06](06-rows-and-recommendations.md) but LLM curation, plus [07](07-client-api.md)'s `GET /home`) | ✅ complete — nine row providers, `HomeService`, the taste centroid, the MovieLens tag genome, `usher derive`. Its live run refuted the *"11× under budget"* p95 at scale ([ADR-0025](decisions/0025-rows-build-sequentially.md)) |
| [2026-08-06-m8-curation.md](../plans/2026-08-06-m8-curation.md) | M8 — Curation ([06](06-rows-and-recommendations.md)'s *LLM curation* section, [10](10-telemetry-and-dashboards.md)'s `llm_calls`, M6's query expansion, the genome's tag vocabulary) | ✅ complete — `OpenAICompatibleClient` (litellm declined, [ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md)), `curated_rows`/`llm_calls`, the candidate pool, `CurationService`, `CuratedProvider` as the tenth provider, `usher curate`. **Two of its own claims were refuted by its live run**: query expansion measured *worse* ([05](05-search-and-similarity.md)) and 88% of generated headings were the genre labels the prompt forbids ([06](06-rows-and-recommendations.md)) |
| [2026-08-10-m9-api-surface.md](../plans/2026-08-10-m9-api-surface.md) | M9 — API surface (all four of [07](07-client-api.md)'s endpoint tables, [10](10-telemetry-and-dashboards.md)'s `search_queries`, the IMDb bulk expansion in [04](04-catalog-bootstrap.md)) | ✅ complete — 74 tasks planned across two tracks (**T4 withdrawn**; **H4/H5 ran late, on 2026-08-12, after the gate**), migrations `m09a`/`m09c`. RFC 9457 over a seven-member closed vocabulary ([ADR-0030](decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)), the playback ticket ([ADR-0029](decisions/0029-the-playback-ticket-changes-the-artifact-not-the-grant.md)), the two-tier suggest ([ADR-0031](decisions/0031-the-two-tier-suggest.md)), the image proxy ([ADR-0032](decisions/0032-the-image-proxy-clamps-to-a-ladder.md)), keyset cursors ([ADR-0034](decisions/0034-the-cursor-carries-a-position.md)). **Two of its own measurements came back as refusals and both shipped as such**: the IMDb entity design failed its size bar so T4 was withdrawn (2.702 GB against 2.0 GB), and the tag-genome similarity term was **removed** at a 2.4746% pair rate against a 10% floor ([ADR-0035](decisions/0035-the-tags-similarity-term.md)). Its third recorded refusal was not one: ✅ **H4/H5's live Emby verification did run, on 2026-08-12** — 23 bounded requests against a real Emby 4.9.5.0, both halves passing, the write to a real account restored byte-for-byte; ⚠️ **after** the gate, because the milestone had concluded "no credentials on this host" from checking one `.env` file and nowhere else. Recorded in [09](09-roadmap.md) |
| [2026-08-18-e1-eval-skeleton-and-suggest.md](../plans/2026-08-18-e1-eval-skeleton-and-suggest.md) | E1 — quality evals, phase 1 of 4 (**not a milestone**): the `usher.eval` skeleton and the suggest surface, from [`docs/specs/2026-08-18-usher-quality-evals-design.md`](../specs/2026-08-18-usher-quality-evals-design.md) | 🔨 in progress — 15 tasks planned; Task 1 landed (the `eval` package, its extra, and the eleventh import contract). E2–E4 get their own plans |
| [2026-08-19-rating-provenance-split.md](../plans/2026-08-19-rating-provenance-split.md) | Rating provenance (**neither a milestone nor an eval phase**): the three `titles` columns written by two sources each, from [`docs/specs/2026-08-19-rating-provenance-split-design.md`](../specs/2026-08-19-rating-provenance-split-design.md) | 🔨 in progress — 7 tasks planned; Task 1 landed: migration `m10a` renames `community_rating`/`vote_count`/`popularity` to `tmdb_*`, adds `imdb_average_rating`/`imdb_num_votes` beside them, and renames `field_provenance`'s three JSONB keys to match — the only statement in the revision that touches a row, and a rename rather than an inference (all 132,415 rows carrying provenance carry all three keys and every value is `tmdb`). No rating **value** is migrated: those are re-imported from `title.ratings.tsv.gz`. Its follow-up also restored `GET /events`' `title.updated` payload to wire field names, which the rename had moved to domain attribute names — the one place a field name travels as data rather than as a key. ✅ **All 7 tasks landed 2026-08-19** — the IMDb writer redirected onto the `imdb_*` pair, `usher bootstrap --phase ratings` added (an alias, not a step, and it refuses an empty catalog), E1's sampling frame re-anchored on `imdb_num_votes`, and the rebuild run against the live 1.27M-title catalog: ADR-0002's frame is restored to **48,639 against 48,549 (+0.19%)** from **8,523** under the contaminated column. The decontamination was the one component that did **not** go as designed: its pre-registered exact-match rule misses 57,701 of 407,860 rows, because the column held 2026-08-11 IMDb values and the re-import brought 2026-08-19 ones, so P3 would have failed. It was reported as an open decision rather than widened unilaterally; the operator then authorised the `enrichment_state` rule, which was applied the same day — **P3 passed at exactly 40,695**, `tmdb_vote_count` fell from 540,275 to the 132,415 enriched rows, and no non-enriched row carries one. [ADR-0040](decisions/0040-rating-columns-name-their-source.md) preserves the sequence. Rollback table `titles_rating_backup_20260819` is intact and must not be dropped without an operator's say-so |
| [2026-08-21-issue-41-resumable-watch-lane.md](../plans/2026-08-21-issue-41-resumable-watch-lane.md) | Resumable watch lane (**neither a milestone nor an eval phase**): the watch-state walk checkpointed on its `StartIndex` so a transient failure costs one page rather than the whole ~5,688-page full-history walk, from [`docs/specs/2026-08-21-issue-41-resumable-watch-lane-design.md`](../specs/2026-08-21-issue-41-resumable-watch-lane-design.md) | ✅ **all 6 tasks landed 2026-08-25/26, branch not yet merged** — `fix/41-resumable-watch-lane`, fixing [#41](https://github.com/anirudhlath/usher/issues/41): `sync_runs` gains a `position`, the newest *incomplete* `WATCH_STATE` run is reclaimed rather than superseded, and `SourceAdapter.watch_state` grows a `start_index`. Why a `StartIndex` checkpoint rather than the `since`-timestamp cursor the issue's wording suggests is [ADR-0042](decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md). **Review added two behaviours the plan did not have**: a non-destructive `save` (`position` only advances, `completed` is absorbing) after the spec's *"no true concurrency exists to race"* was measured false, and merges stamped with the attempt's instant rather than the reclaimed run's. ⏳ **The walk itself has still never been run to completion** — that is the operator's step after this merges (nothing schedules one), and the branch review measured convergence at 3–10 attempts |

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
