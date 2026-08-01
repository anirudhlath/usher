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
| **M3 — Emby adapter** ✅ | Durable-client auth, item listing, watch-state read/write, stream targets; adapter contract tests, run against both a pure in-memory adapter and the real one, plus a live-server verification pass |
| **M4 — Ingest pipeline** ✅ | Ingest → match → enrich; priority queue; stub-on-sight; unmatched review; the availability sweep and its refusal. **The `index` stage is M6's** — see the boundary calls below |
| **M5 — Push and read-through** | WebSocket events, reconnect/reconcile, demand promotion, SSE to clients |
| **M6 — Search** | **The `index` stage of [03](03-sources-and-sync.md)'s pipeline**; full-text, autocomplete path, embeddings, RRF fusion, similarity, neighbour precompute |
| **M7 — Rows** | Row and RowProvider hierarchy, system rows, similarity rows, taste centroid |
| **M8 — Curation** | LLM row generation, validation, persistence, regeneration job |
| **M9 — API surface** | Full HTTP surface, image proxy, playback resolution, **the playback ticket that succeeds [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md)**, attribution |
| **M10 — Hardening** | Observability, failure modes, backup/restore, docs, public release |

TV is in scope throughout, not deferred — series/season/episode modelling,
Next Up, and episode-level watch state land with the milestones that own them.

**M4's boundary was ambiguous in four places, and each was decided
deliberately rather than drifted into.**

1. **The `index` stage is not built; M6 owns it.** [03](03-sources-and-sync.md)
   stage 4 is "update the search document and compute the embedding", and
   neither artefact exists before M6 — no search-document column, no
   `title_embeddings` table, no embedder. M4 therefore ships **no `index` job
   kind at all**: a job kind whose handler is a stub is a queue that grows
   forever. M6 adds the kind, the handler and one backfill enqueue over
   `titles`. PRD 03 itself licenses this — that stage "is a pure function of
   catalog state and can be rebuilt from scratch at any time".
2. **Enrichment populates `Title`, `Season` and `Episode` only.**
   `Person`, `Credit`, `Collection` and `Image` are first *read* by M7 (rows)
   and M9 (the image proxy), and each is re-derived from the verbatim
   `raw_payloads` cache with **no second network call** — which is what
   [ADR-0016](decisions/0016-raw-payloads-cache-providers-not-sources.md) says
   that table is for. `EnrichmentResult` is a frozen dataclass, so adding
   `people: tuple[Person, ...]` later is an added field, not a signature
   change.
3. **Push, reconnect-delta, demand promotion and SSE are M5.** M4 builds the
   full-reconcile lane and the cursor-driven delta lane. The queue's promotion
   *mechanism* (`ON CONFLICT … SET priority = GREATEST(…)`) lands in M4
   because it is one clause of the enqueue statement, but nothing in M4 calls
   it with `JobPriority.DEMAND`.
4. **No HTTP route is added.** `POST /admin/sources/{id}/sync`,
   `GET /admin/unmatched` and `POST /admin/unmatched/{id}/resolve` are all
   M9's surface, where [07](07-client-api.md)'s error-envelope vocabulary gets
   defined. M4 delivers the same capability through `usher.cli` — the
   project's established second composition root, exactly as M2 did for
   `bootstrap` — and every service is constructed identically by both roots,
   so M9 adds routers over finished wiring.

**What M4 leaves M5 to build on:** a queue with a real claim/park/backoff
contract and a `traceparent` column, an `IngestService` that takes a `since`
cursor, and a `WatchStateSyncService` whose merge is safe to call with a
*partial* state — which is exactly what a `UserDataChanged` push event hands
it.

**M4 was live-verified against both of its upstreams**, on 2026-07-31 against
an Emby 4.9.5.0 server and on 2026-08-01 against TMDb's v3 API — the latter
being the first request this project has ever made to `api.themoviedb.org`,
since every TMDb fixture until then was a transcription of documentation.
Both runs found real defects the fakes could not:  Emby's watch-state
write-back route was simply wrong, and TMDb turned out to classify two
permanent 4xx failures as retryable outages and to filter search years
*exactly* where the match ladder filters ±1. `CLAUDE.md` carries both runs
guess by guess, including what remains unverified. **One measured
opportunity is recorded and deliberately not taken:**
`append_to_response=season/N` collapses a series' enrichment from 1+N
requests to 1, which is the difference between ~190k and ~35k requests for a
full pass — it changes [03](03-sources-and-sync.md)'s request table and
[04](04-catalog-bootstrap.md)'s crawl arithmetic and belongs in its own
change.

**M9 owes ADR-0012 a successor.** In v1, `POST /titles/{id}/play` returns a
target URL carrying the source's session token, because M3 has no HTTP surface
to redirect from. M9 builds that surface, so M9 is where the opaque,
short-lived playback ticket lands — a `302` to the real URL, which makes the
shareable artifact opaque rather than removing the grant. Listed here and not
only in the ADR that defers it: an obligation recorded only where it was
postponed is one nobody plans.

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
