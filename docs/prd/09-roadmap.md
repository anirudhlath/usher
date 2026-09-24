# 09 — Roadmap

> 🔶 **Provisional past M10.** M1–M10 are complete; *Post-v1 candidates*
> below are not committed to.

## v1 — the abstraction works end to end

Success condition: a client can be built against Usher that fully replaces
direct Emby access, for both movies and television.

⚠️ **"v1" here is a scope name, not a version number.** The first tagged
release is `v0.1.0`; meeting this heading's success condition is not a
compatibility promise.

| Milestone | Contents |
|---|---|
| **M1 — Foundation** ✅ | Repo, uv project, compose, Postgres + migrations, domain models, port ABCs, config, health, CI with layering checks, telemetry bootstrap |
| **M2 — Bootstrap** ✅ | IMDb skeleton, TMDb ID export, Wikidata crosswalk; resumable importers |
| **M3 — Emby adapter** ✅ | Durable-client auth, item listing, watch-state read/write, stream targets; adapter contract tests run against both a fake and the real adapter, plus a live-server verification pass |
| **M4 — Ingest pipeline** ✅ | Ingest → match → enrich; priority queue; stub-on-sight; unmatched review; the availability sweep and its refusal |
| **M5 — Push and read-through** ✅ | WebSocket events with health grounded in a message ledger, supervised reconnect with a gap-closing delta, demand promotion, `GET /titles/{id}`, SSE to clients over an `EventPublisher` port, and two supervised lanes in the server process |
| **M6 — Search** ✅ | The `index` stage of [03](03-sources-and-sync.md)'s pipeline; a weighted full-text document as a generated column, a typo-tolerant autocomplete path on its own port, optional embeddings with a fingerprint that makes staleness a query, RRF fusion reporting its own coverage, similarity, and a precomputed neighbour table. Meilisearch was not adopted; the two-tier suggest (M9) took its place |
| **M7 — Rows** ✅ | `Row`/`RowProvider` as ports and nine registered providers, `HomeService`'s propose→score→diversify→build, in-process row and screen caches, the taste centroid and genre affinity, `GET /home` and `usher home`, `row.invalidated`, the MovieLens tag-genome importer, and `Person`/`Credit`/`Collection` re-derived from `raw_payloads` with no second network call |
| **M8 — Curation** ✅ | LLM row generation, validation, persistence and regeneration — `OpenAICompatibleClient` over httpx, `curated_rows` + `llm_calls`, `CandidatePoolService`, `CurationService`, `RowFamily.CURATED` + `LLMRow` + `CuratedProvider` as the tenth provider, `JobKind.CURATE`, `POST /admin/rows/regenerate` and `usher curate`. Plus query expansion (shipped off) and the genome's tag vocabulary |
| **M9 — API surface** ✅ | Full HTTP surface, image proxy, playback resolution and the playback ticket, outbound watch state, [07](07-client-api.md)'s RFC 9457 error envelope, `GET /titles/{id}/similar`, the `search_queries` analytics table, the two-tier suggest, attribution. Also the three ranking terms M7 built data for — taste-centroid proximity, watch state and recency — and the removal of the tag-genome similarity term |
| **M10 — Hardening** ✅ | Observability, failure modes, backup/restore, docs, and the public release, `v0.1.0`. Plus the scheduler: a small set of named jobs with a period rather than a general cron, **no scheduler table** — each job reads *when was I last done* off the artefact it maintains — and `USHER_SCHEDULER_ENABLED=false`, because there is no mutual exclusion without a row and the job it would start is hours long. `usher schedule --once` keeps the operator's-own-cron path supported |

TV is in scope throughout, not deferred — series/season/episode modelling, Next
Up, and episode-level watch state land with the milestones that own them.

## Carried debt — found by a milestone, owned by none

Known open issues that no milestone owns.

- **Columns that leak a raw driver exception across the port boundary.** Almost
  all of them are the staged `COPY` path. The per-column ledger is in
  `scripts/audit_bounded_columns.py`.
- **Query expansion ships off.** Post-v1 unless `search_queries` supplies a
  real evaluation set ([05](05-search-and-similarity.md)).
- **`GET /admin/unmatched` sorts the whole unmatched population for every
  page.** A covering index on `(added_at DESC NULLS LAST, id DESC) WHERE
  title_id IS NULL` would remove the sort; none exists.
- **The `MissingGreenlet` crash that ended a worker is still unexplained.**
  Both worker roots record a crashed pass with its frames; the diagnosis is
  owed, and the crash itself has no ticket.
- **The suggest evaluation's three `fuzzy recall_at_5` bars stay pending**,
  blocked on the ordering defect in [05](05-search-and-similarity.md).
- **The watch-state walk has never been run to completion.** The lane is
  resumable, nothing schedules one, and the first full walk is the operator's
  step ([03](03-sources-and-sync.md)).

## Post-v1 candidates

Not committed; recorded so the design keeps room for them.

- **Authentication** — real user accounts and per-client tokens through the
  seam left in [01](01-architecture.md).
- **Additional sources** — Jellyfin and Plex adapters. The genuine test of the
  abstraction is whether these require no change outside their packages.
- **Additional metadata providers** — OMDb for aggregated ratings, TVDb for
  alternate episode orderings, resolved through `field_provenance`.
- **Alfred integration** — media intents, spoken row reasons, voice-driven
  playback.
- **Meilisearch** — behind the `SuggestIndex` port. Adopting it needs one
  `SuggestIndex` implementation **plus a write path**, which the port does not
  have today.
- **Request/wanted list** — titles in the catalog but on no source.

**The reference client is done, and not as a separate repository.** Usher
Console lives in `web/`, in this repository and in the same container, served
at `/console`. The behavioural authority is `web/docs/patterns.md`.

## Explicitly out of scope

Transcoding, file management, downloading or acquisition, multi-tenant hosting,
commercial use, and collaborative filtering.
