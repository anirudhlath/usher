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
| **M5 — Push and read-through** ✅ | WebSocket events with health grounded in a message ledger ([ADR-0018](decisions/0018-push-health-is-a-message-ledger.md)), supervised reconnect with a gap-closing delta, demand promotion, `GET /titles/{id}`, SSE to clients over an `EventPublisher` port ([ADR-0019](decisions/0019-the-client-event-channel-is-a-port.md)), and two supervised lanes in the server process |
| **M6 — Search** ✅ | **The `index` stage of [03](03-sources-and-sync.md)'s pipeline**; a weighted full-text document as a generated column, a typo-tolerant autocomplete path on its own port, optional embeddings with a fingerprint that makes staleness a query, RRF fusion reporting its own coverage, similarity, and a precomputed neighbour table. **Adds no HTTP route and no new client event, both deliberately** — see the boundary calls below. **[ADR-0002](decisions/0002-postgres-first-search.md)'s Meilisearch gate ran against a real 1,271,138-title catalog on 2026-08-03 and failed for short names and for latency**; the follow-up is the two-tier suggest, owned by M9 |
| **M7 — Rows** | Row and RowProvider hierarchy, system rows, similarity rows, taste centroid. **Plus the MovieLens tag-genome importer** — five documents specify it and nothing builds it; see below |
| **M8 — Curation** | LLM row generation, validation, persistence, regeneration job |
| **M9 — API surface** | Full HTTP surface, image proxy, playback resolution, **the playback ticket that succeeds [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md)**, **outbound watch state (`PUT /watch/titles/{id}` and the source write-back retry job)**, **[07](07-client-api.md)'s RFC 9457 error envelope**, `GET /titles/{id}/similar` over M6's precomputed table, **the `search_queries` analytics table whole** ([10](10-telemetry-and-dashboards.md)), **the two-tier suggest [ADR-0002](decisions/0002-postgres-first-search.md)'s failed gate obliges** (see below), attribution |
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
requests to 1 — at 32,409 series and a measured median of 9 seasons, ~324k
requests against ~32k, i.e. **~10x** on the series half of a full pass. (The
"~190k → ~35k, ~5x" first recorded here was arithmetically impossible;
[04](04-catalog-bootstrap.md) and `CLAUDE.md` carry the correction and where
the wrong figure came from.) It changes [03](03-sources-and-sync.md)'s
request table and [04](04-catalog-bootstrap.md)'s crawl arithmetic and
belongs in its own change.

**M5's boundary was ambiguous in six places, and each was decided
deliberately rather than drifted into.**

1. **Exactly two client-facing routes: `GET /titles/{id}` and `GET /events`.**
   Demand promotion is *defined* by [03](03-sources-and-sync.md) as a
   property of requesting a title, so without a title route it is a
   mechanism with no caller. Everything else on [07](07-client-api.md)'s
   surface stays where this roadmap puts it.
2. **`GET /titles/{id}` returns metadata, availability and watch state** —
   not credits, images, similar titles or the season/episode hierarchy, each
   of which belongs to a milestone that has not run. `"credits": []` for a
   title whose credits have not been derived is indistinguishable, to a
   client, from a title with no cast.
3. **Outbound watch state is not built; M9 owns it.**
   `SourceAdapter.push_watch_state` exists and is live-verified; what is
   missing is the caller, a fourth `JobKind`, and a conflict rule against
   inbound push arriving in the same second.
4. **A push `ITEM_REMOVED` retracts nothing.**
   [ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)
   is unambiguous, and an Emby library refresh emits `ItemsRemoved` for
   items that have not gone anywhere. M5 counts and logs it.
5. **The client event bus is in-memory and in-process, so the server
   process runs the job worker** (`worker_enabled`), which is what closes
   PRD 03's read-through loop in the shipped deployment.
   [ADR-0019](decisions/0019-the-client-event-channel-is-a-port.md).
6. **[07](07-client-api.md)'s RFC 9457 envelope stays deferred to M9.** A
   stream's failure vocabulary is a format for an *event*, not a response
   body, and M5 has no request whose honest answer is a domain-level
   failure — `GET /titles/{id}` is answerable entirely from local state.

**M5 was live-verified against the same Emby 4.9.5.0 server on 2026-08-02**,
and it is the first run in this repository ever to have parsed a real
`/embywebsocket` message: every push fixture before it was transcribed from
Emby's DTOs. It confirmed the `UserDataChanged` envelope and payload, refuted
three guesses about it, and measured the `Sessions` interval that
`push_stale_after_seconds` rests on. `CLAUDE.md` carries it guess by guess.

**M6's boundary was ambiguous in nine places, and each was decided
deliberately rather than drifted into.**

1. **No HTTP route is added.** `usher index`, `usher search`, `usher suggest`
   and `usher similar` deliver every capability through the CLI — the
   project's established second composition root, exactly as M2 did for
   `bootstrap` and M4 did for the whole ingest pipeline. **PRD 05's
   `GET /titles/{id}/similar` is M9's**, and M6 builds the service and the
   precomputed table it will read. M5 added two routes because demand
   promotion is *defined* as a property of requesting a title and had no
   caller otherwise; nothing in M6 has that shape, and M7 is the in-process
   consumer of similarity.
2. **Weight class B — cast and crew — ships reserved and empty, and
   [05](05-search-and-similarity.md) is corrected to say so.** There is no
   `Person`, `Credit`, `Collection` or `Image` table, model or port anywhere
   in `src/` (M4's boundary call 2, above). The only place credits physically
   exist is `raw_payloads.payload`, and building a search document out of a
   *provider's* JSON shape would put a TMDb-shaped concept in `services/`.
   Because the document is a generated column, filling B when M7 lands
   `Credit` is a migration rather than a rewrite.
3. **No `title_search_names` table is built.** [05](05-search-and-similarity.md)
   specifies a narrow `(title_id, name, kind, popularity)` table for
   autocomplete, justified by *aliases and people names* — one title
   contributing many rows. Neither has a data source in M6, so the table
   would hold one row per title duplicating four columns of `titles`: a
   second copy, and a new instance of the staleness problem this milestone
   exists to eliminate. The trigram index goes directly on `titles`. When M7
   lands aliases and people, the narrow table is the migration that adds them.
4. **Embeddings cover the enriched tier, not the 1.27M-row catalog.** This is
   [05](05-search-and-similarity.md)'s own two-workload split taken
   seriously. The population is `enrichment_state <> 'skeleton'`, for which
   `ix_titles_enrichment_state` is already exactly the partial index — and a
   skeleton title needs no `index` job at all, because its search document is
   a generated column. At the measured throughput that is the difference
   between 4–6 hours and 25 seconds to 2 minutes.
5. **M6 publishes no new client event, and this row's own wording is
   corrected.** It used to end *"Publishes `title.updated` through the
   `EventPublisher` port M5 built rather than inventing a channel"* — **false
   in its first clause and true in its second**. `EnrichService` already
   publishes `title.updated` after the commit, naming the changed fields: M5
   built the port *and its caller*. Nothing a client renders depends on the
   search document or the embedding, so a second publication on index
   completion would be an event with no consumer, which `ports/events.py`
   establishes as the thing this project does not do. **M6 satisfies the
   intent of the row by *not* inventing a channel.**
6. **Query expansion is not built.** [05](05-search-and-similarity.md) names
   one LLM call rewriting an emotional query into narrative language as the
   cheaper, better-evidenced lever for mood queries. It is one call in front
   of an existing `embed`, `ports/llm.py` has no implementation until M8, and
   a second unimplemented port dependency on the search path buys nothing M6
   can measure. M6 embeds the query as typed; the seam is
   `SearchService.search`'s query string.
7. **Meilisearch is not added regardless of what the gate says — and the gate
   ran on 2026-08-03 and failed.** Over 2,993 single-edit typo cases on 750
   real catalog names, the shipped type-ahead finds the right title **27.8%
   of the time for a 2–4-character name** and **68.3% for a 5–7-character
   one**, against a bar of 0.75 and 0.85 written down beforehand, and no
   configuration comes within 6× of an as-you-type latency budget. Above 8
   characters it is 95–100% and needs nothing.
   [ADR-0002](decisions/0002-postgres-first-search.md) carries the full
   result. M6's deliverable is exactly what this call said it would be: the
   recorded failure, the ADR amended, and a scoped follow-up — **the two-tier
   suggest, owned by M9** (below) — not a second stateful service bolted on
   at the end of a milestone. The `SuggestIndex` port
   ([ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)) is what
   keeps a later Meilisearch at one class plus a write path. The gate's
   *definition* was also incomplete — recall@5 only, and recall is the half
   that passes — so the run measured latency per cell as well.
8. **Similarity blends the two signals that have data, and says out loud that
   the other two do not.** [05](05-search-and-similarity.md) specifies a
   four-way blend; checked against the code, cast/crew Jaccard has no
   `Credit` table (2), the MovieLens tag genome has no importer (below), and
   `titles.collection_id` is a bare nullable UUID with no table that nothing
   in `src/` writes. So M6 ships embedding cosine plus genre and keyword
   Jaccard, written as a sum of weighted terms over an explicit signal list,
   so that landing a third signal is a term and a weight rather than a
   rewritten scorer.
9. **The `usher.db.staging` shared-table lock is fixed here, because M6 is
   what makes it hurt.** M5 recorded it; M6 adds an `index` enqueue to
   `EnrichService`'s hot path, which is one job per enriched title through a
   table-level exclusive lock. Fixing it in M6 is fixing it where the cost
   arrived.

**M6 is complete, gate included.** The gate ran against a real 1,271,138-title
catalog on 2026-08-03 and **failed for short names and for latency**; the
number is recorded in [ADR-0002](decisions/0002-postgres-first-search.md) and
[05](05-search-and-similarity.md), one shipped default changed on the strength
of it (a `vote_count` tiebreak in the suggest ordering, because
`titles.popularity` is NULL on every row of a bootstrapped catalog), and the
follow-up has an owner below. A failed gate that is written down with its
numbers is a closed item, not an open one.

### The follow-up the gate obliges: a two-tier suggest, owned by M9

**Owner: M9**, alongside the HTTP surface and the `search_queries` table,
because a debounce and a tier split are properties of the *request* boundary
and M6 deliberately adds no route (boundary call 1).

- **Tier 1 — btree `lower(name) text_pattern_ops` prefix, on every
  keystroke.** Measured over the gate's own 2,993 queries at **p50 0.6 ms /
  p95 1.0 ms / max 10 ms**, a 44 MB index that builds in 0.559 s over
  1,271,138 rows. It is the only thing measured that fits inside a keystroke
  budget, and it has **no typo tolerance at all** (1.9%).
- **Tier 2 — the existing trigram + `levenshtein_less_equal` path,
  debounced behind it.** Unchanged code; what changes is that it stops being
  asked to answer in 50 ms. At a debounce-tolerable budget the
  recall-maximising configuration is a real choice again, and the gate
  already priced it: GiST KNN plus the vote-count tiebreak is 85.3% at p95
  304 ms against GIN's 82.5% at 211 ms — **but the two indexes cannot
  coexist**, because a GiST trigram index present alongside the GIN one makes
  the planner take GiST for `%` and costs the shipped path 4.3× on p50 for
  identical recall. Choosing GiST means *replacing* GIN, and that decision
  belongs with the tier split rather than ahead of it.
- **What would make the choice on evidence rather than on this run:**
  [10](10-telemetry-and-dashboards.md)'s `search_queries` table, also M9's.
  Every case in the gate is a synthetically mutated real title; nobody has
  yet measured what this catalog's users actually type.

**The MovieLens tag genome has no owner, and now it has one: M7.** This is
the one item in this section that is not about M6 at all, and it is the most
important, because it is an obligation that was recorded only where it was
*assumed*:

| Where it is promised | What it says |
|---|---|
| [01](01-architecture.md) ports table | `BulkDataset` → …, `MovieLensGenome` |
| [02](02-data-model.md) supporting tables | `genome_scores` — "MovieLens tag-genome relevance vectors, where available" |
| [04](04-catalog-bootstrap.md) | a phase, a size, a runtime, **and a licence row** |
| [05](05-search-and-similarity.md) similarity | "MovieLens tag-genome cosine where available (~7% coverage)" |
| [06](06-rows-and-recommendations.md) | names it as a similarity input |

Against that: `PHASES` in `usher.cli` is `("imdb", "tmdb-ids", "crosswalk",
"all")` with **no `movielens`**, and `adapters/bulk/` holds `imdb.py`,
`tmdb_ids.py` and `wikidata.py` with **no `movielens.py`**. Five documents
assume it exists and, until this paragraph, **no document said when it would**
— not this roadmap, not `CLAUDE.md`, not the progress log. It is the identical
failure this roadmap already names for ADR-0012, in a sentence worth reusing
verbatim: **an obligation recorded only where it was postponed is one nobody
plans.** Here it is worse, because it was recorded only where it was assumed.

**M6 does not build it.** It is an M2-shaped bulk importer — a dump, a
resumable cursor, a `BulkDataset` implementation, a `genome_scores` table, a
licence row — and [04](04-catalog-bootstrap.md) owns the phase. Folding it
into a search milestone at the end would be a second unplanned milestone
inside this one.

**M7 owns it**, because M7 is the milestone that *reads* similarity signals
and is therefore the first whose output is measurably worse without it
([06](06-rows-and-recommendations.md) names it as a row input). What it costs:
one `BulkDataset` implementation, one entry in `PHASES`, one table, one
licence row in [04](04-catalog-bootstrap.md), and one weighted term in
`SimilarityService`'s existing signal list. **And if M7 also declines it**:
[05](05-search-and-similarity.md)'s ~7% coverage figure stays a plan rather
than a measurement, and its four-way blend stays a two-way one. Said out loud,
rather than left for a table of four bullets to imply four shipped signals.

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
- **Meilisearch** — **the typo-tolerance gate in
  [05](05-search-and-similarity.md) ran on 2026-08-03 and failed**, so this is
  now a justified candidate rather than a hypothetical one. It is still not
  taken in v1: the cheaper answer the same run measured is the two-tier
  suggest above, and nothing has yet established that Meilisearch would do
  better on *this* catalog — the head-to-head the ADR's Uncertainty section
  names still does not exist. Cost if taken:
  [ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)'s port split
  keeps it at one `SuggestIndex` implementation **plus a write path**, and
  that write path is the dual write
  [ADR-0002](decisions/0002-postgres-first-search.md) refused — which is
  exactly why the port has no write method today.
- **Reference client** — separate repository.
- **Request/wanted list** — titles in the catalog but on no source.

## Explicitly out of scope

Transcoding, file management, downloading or acquisition, multi-tenant hosting,
commercial use, and collaborative filtering.
