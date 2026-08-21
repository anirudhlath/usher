# 09 — Roadmap

> 🔶 **Provisional — and the reason it says so is not the reason it used to.**
> This marker read *"will be refined once [07](07-client-api.md) and
> [08](08-operations.md) are brainstormed and the implementation plan is
> written"* until 2026-08-12. All three happened: both documents are ✅ agreed,
> nine plans exist in [`docs/plans/`](../plans/), and M1–M9 are complete and
> refined against what they measured. What is still provisional is **M10 and
> everything after it** — M10 has no plan, and *Post-v1 candidates* below is a
> list of things deliberately not committed to. The marker stays; its stated
> condition was discharged and leaving the old sentence would have been a
> "verified" fact that stopped being one.

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
| **M7 — Rows** ✅ | `Row`/`RowProvider` as ports and **nine registered providers**, `HomeService`'s propose→score→diversify→build, in-process row and screen caches, the taste centroid and genre affinity, `GET /home` and `usher home`, `row.invalidated`. **Plus the MovieLens tag-genome importer** — five documents specified it and nothing built it, and it is now the similarity blend's third live signal. Plus `Person`/`Credit`/`Collection` re-derived from `raw_payloads` with no second network call (`usher derive`), search weight class B filled, and two measurements the milestone owed: the sequential build's cost, and `titles.popularity`/genome coverage with their denominators |
| **M8 — Curation** ✅ | LLM row generation, validation, persistence, regeneration job — `OpenAICompatibleClient` over httpx ([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md), litellm declined), `curated_rows` + `llm_calls` (migration `m08a`), `CandidatePoolService`, `CurationService`'s assemble→call→validate→persist with the validator as its own module, `RowFamily.CURATED` + `LLMRow` + `CuratedProvider` as **the tenth row provider**, `JobKind.CURATE` + handler, `POST /admin/rows/regenerate` (202), and `usher curate`. **Its eight boundary calls are below**, and 🔴 **the product finding is below them**: 88% of one live run's headings were the genre labels the prompt forbids. **Inherits from M7:** `curated_rows`, `LLMRow`, `CuratedProvider`, `RowFamily.CURATED` and `POST /admin/rows/regenerate` as one family (call 2); M6's query expansion (its call 6) — ✅ **shipped by Task 20 and then measured**: against a local `gemma-4-26b-a4b` it moved MRR **0.733 → 0.373**, so it ships behind `USHER_QUERY_EXPANSION_ENABLED`, off by default and independent of `USHER_LLM_ENABLED` ([05](05-search-and-similarity.md)); and the genome's **tag vocabulary**, which M7 deliberately did not store — a prompt that wants to say "atmospheric, thought-provoking" needs the words, and `genome_revision` is what made loading them later safe. ✅ **Shipped by Task 19**: `genome_tags(tag_id, tag, genome_revision)`, migration `m08b`, 1,128 rows loaded by the same `bootstrap --phase movielens`, with `GenomeRepository.vocabulary(revision)` refusing a release mismatch ([02](02-data-model.md), [04](04-catalog-bootstrap.md), [ADR-0024](decisions/0024-the-genome-is-one-dense-vector-per-title.md)) |
| **M9 — API surface** ✅ | Full HTTP surface, image proxy, playback resolution, **the playback ticket that succeeds [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md)**, **outbound watch state (`PUT /watch/titles/{id}` and the source write-back retry job)**, **[07](07-client-api.md)'s RFC 9457 error envelope**, `GET /titles/{id}/similar` over M6's precomputed table, **the `search_queries` analytics table whole** ([10](10-telemetry-and-dashboards.md)), **the two-tier suggest [ADR-0002](decisions/0002-postgres-first-search.md)'s failed gate obliges** (see below), attribution. **Inherits from M7:** artwork on `RowCard` with the `Image` table and `GET /images/{id}` (call 3); `title_search_names`' *people* half, with M6's condition **restated rather than renewed** (call 6); row provider enable/disable via the admin API (call 9); the RFC 9457 envelope `GET /home` deliberately ships without (call 1), plus `usher.http.server.duration`, `usher.cache.hits`/`.misses` and serve-stale-while-refreshing; `credits` as a key on `GET /titles/{id}`; the three ranking terms M7 built data for and did not wire — taste-centroid proximity, watch state and recency ([05](05-search-and-similarity.md)); and **the tag-genome weight M7 left at 0.25 on coverage that does not support it** — ✅ **settled 2026-08-12 by S7 and the answer is the revert**: the priority tier was enriched and embedded so the pair rate could be measured over a population whose documents carry weight classes C and D, S5's one pool walk put it at **2.4746%** (323,297 of 13,064,700 pairs over 130,647 seeds) against the **10%** floor, and the term is removed from `SimilarityService._WEIGHTS` while the vectors, the pair read and the rebuild's coverage counters stay ([ADR-0024](decisions/0024-the-genome-is-one-dense-vector-per-title.md)). It is a **second measurement, not a rise from 1.81%** — S1 established M7's figure came from 5,020 name-selected seeds in a database that no longer exists. **The blend change obliged one `usher similar --rebuild`, which S7 did not run and H7 did** — 2026-08-12, 88.3 minutes over 130,647 seeds, `stale_neighbors()` **0** against **3,266,175 rows** all stamped `78f3ecd2…`, the row count recorded beside the verdict because an empty table reports no stale rows too and empty is what it was. **Complete 2026-08-12 on `milestone/m9-api-surface`: 74 tasks planned across two tracks, migrations `m09a` and `m09c`, six ADRs (0029–0032, 0034, 0035).** T4 was withdrawn when the IMDb entity design failed its own pre-registered size bar. ✅ **H4 and H5 — the live Emby verification of `/play` → ticket → `302` → a real 206, and of the watch write-back round trip read back from Emby — ran on 2026-08-12 and both passed**, in 23 bounded requests with no walk, the write restored byte-for-byte; ⚠️ they ran **after** the gate, because the milestone shipped them as an unrunnable gap having checked one `.env` file and nowhere else. See *M9's boundary calls* below. **M9's eight boundary calls are below** |
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
   so M9 adds routers over finished wiring. ✅ **`POST /admin/sources/{id}/sync`
   shipped by M9's E3**, as `JobKind.SYNC` and an enqueue rather than a
   synchronous walk — M8's `POST /admin/rows/regenerate` had already settled
   that shape, and the reason first written down here ("there is no
   reconciler until M5") was already wrong by M4 itself. The other two routes
   in this item remain open.

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
opportunity was recorded and deliberately not taken, and M9 took it:**
`append_to_response=season/N` collapses a series' enrichment from 1+N
requests to 1 — at 32,409 series and a measured median of 9 seasons, ~324k
requests against ~32k, i.e. **~10x** on the series half of a full pass. (The
"~190k → ~35k, ~5x" first recorded here was arithmetically impossible;
[04](04-catalog-bootstrap.md) and `CLAUDE.md` carry the correction and where
the wrong figure came from. The median's sample is 30 popular-skewed series,
so ~324k is an upper bound on that measurement rather than a prediction of
what the 1+N shape would have cost a real catalog.) It changed
[03](03-sources-and-sync.md)'s request table,
[04](04-catalog-bootstrap.md)'s crawl arithmetic and
`TmdbMetadataProvider.fetch` — which is precisely why M4 left it to its own
change, and M9 is where that change happened. It gave up one property in the
process: a season TMDb refuses can no longer park the job, because an absent
season and an omitted one are the same `200`. Recorded with the shipped code
rather than only here.

**And the deferral itself is worth one sentence, because this file is where
that lesson keeps landing.** The call was recorded in three places and still
sat four milestones, then had to be found by a reviewer rather than picked up
by a plan: nothing in M9's own 74-task breakdown claimed this paragraph, so
the change shipped and the retrospective describing it as "not taken" would
have survived it. A deferral recorded in a retrospective has no owner, which
is the same failure as *"a document with no markers is not a document with no
gaps"* below, one level up — in the ledger rather than in the spec.

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

   **M7 filled it, and annotated that last sentence rather than deleting
   it.** It is true of the search *path* — no service was rewritten — and
   understates the migration: filling B needed a denormalised
   `titles.credit_names` column (a stored generated expression cannot reach
   another table, and the `IMMUTABLE`-wrapper workaround is accepted by
   Postgres and silently wrong), a forced full-column rewrite in the same
   migration, and a re-embed of the whole enriched tier, because the
   positional assembly moves every fingerprint including uncredited titles'.
   See [05](05-search-and-similarity.md).
3. **No `title_search_names` table is built.** [05](05-search-and-similarity.md)
   specifies a narrow `(title_id, name, kind, popularity)` table for
   autocomplete, justified by *aliases and people names* — one title
   contributing many rows. Neither has a data source in M6, so the table
   would hold one row per title duplicating four columns of `titles`: a
   second copy, and a new instance of the staleness problem this milestone
   exists to eliminate. The trigram index goes directly on `titles`. When M7
   lands aliases and people, the narrow table is the migration that adds them.
   **Settled in M9 (`m09a`), and the duplication is still refused**: the table
   holds only `alias` and `person` rows — no `primary` — so `titles` remains
   the sole home of a canonical name, and `popularity` is dropped from the
   column list with the measurement that killed it (NULL on all 1,271,138
   rows). It gains `region` and `language`, which the sketch here did not
   have. The trigram index on `titles` stays exactly as M6 built it.
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

   **M8 built it, and the annotation is the measurement rather than the
   delivery.** `QueryExpansionService` ships into that seam — and run on
   2026-08-07 against a local `gemma-4-26b-a4b` over five mood queries and 150
   real overviews it moved MRR **0.733 → 0.373** and recall@10 **0.800 →
   0.533**, so it ships behind `USHER_QUERY_EXPANSION_ENABLED`, default off and
   independent of `USHER_LLM_ENABLED`. M6's *"buys nothing M6 can measure"* was
   the right instinct for the wrong reason: the cost of not measuring was not
   a deferred improvement, it was eight milestones of a PRD claim nobody had
   checked. See [05](05-search-and-similarity.md) for the numbers, the
   label-free control, and the caveat — one model, one 150-document corpus,
   five queries. A real evaluation set is **M9's** `search_queries`.
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

**M7's boundary was ambiguous in nine places, and each was decided
deliberately rather than drifted into.** They were **recorded as they were
executed** rather than in one pass at the end, so call 8 below landed with the
commit that made it and the other eight arrived with the milestone's
documentation task — which is why 8 reads longer than its neighbours and
carries a measurement they do not.

1. **`GET /home` IS built, and it is the first client-facing route since M5.**
   M6 declined routes and delivered through the CLI, and three milestones
   before it did the same, so the default here is CLI-only. What overrides it
   is that [ADR-0006](decisions/0006-server-composed-home.md) is the ADR
   governing this milestone and its entire content is about the route — and
   its central claim, *"one request paints a screen"*, is a property of a
   **request boundary** that no command can exhibit. M6's call 1 declined
   routes for capabilities the CLI genuinely delivers (`search`, `suggest`,
   `similar` each print exactly what the route would return); composition is
   the first capability where the CLI is a *proxy* for the deliverable rather
   than the deliverable in another skin. `row.invalidated` is assigned to M7
   by name in [07](07-client-api.md)'s SSE table, and an invalidation event
   with no row to invalidate is an event with no consumer. `usher home` ships
   too, because [08](08-operations.md)'s operator rule is that every command
   works against an empty database, and a route is a poor place to discover
   that composition divides by zero on a household that has watched nothing.
   **What stays M9's, so this is a route and not a land grab:** the RFC 9457
   envelope, `usher.http.server.duration`, `usher.cache.hits`/`.misses`, HTTP
   cache headers, and pagination. The screen comes back in one response with
   **no cursor** — what ADR-0006 specifies, and what [07](07-client-api.md)'s
   own endpoint table already showed.

2. **`curated_rows`, `LLMRow` and `CuratedProvider` are not built. M8 owns the
   family whole.** [02](02-data-model.md) already said so and
   [06](06-rows-and-recommendations.md) says `LLMRow.build()` *"only hydrates
   stored output"* — so hydrating a table whose generator does not exist would
   fix that table's shape before anything had tried to fill it, which is the
   `search_queries` failure [10](10-telemetry-and-dashboards.md) argues at
   length, one milestone early. `POST /admin/rows/regenerate` goes with it, and
   `RowFamily` ships with **two** members rather than a `CURATED` nobody can
   emit: a cap over a family with no members is a branch no test can reach.
   Nine of [06](06-rows-and-recommendations.md)'s ten providers ship, and its
   table is annotated rather than silently shipped short.

3. **`RowCard` carries no artwork field, exactly as `GET /titles/{id}` carries
   no `images` key.** ✅ **Discharged in M9, which is the outcome this call
   named rather than a reversal of it.** There is no `Image` table, no `images`
   column and not even a `poster_path` on `titles`; M9 owns the proxy and the
   table. The choice was between an always-null field and no field, and M5
   settled the identical question one route over in a sentence this call reuses
   verbatim: *"an empty list would be indistinguishable from a film with no
   cast."* A card with `"artwork": null` on every row is a client-side branch
   that never takes its other arm, and the day M9 fills it every client that
   shipped against the null already renders without it. M9 built the table, the
   derivation and the proxy, and `RowCard.artwork` is now **one image id chosen
   against the row's `display_hint`** — additive, with both arms of the branch
   reachable. `rating` is refused on the
   same page for a different reason — see
   [06](06-rows-and-recommendations.md): `watch_states` has no `rating` and no
   `favorite`, and neither does `SourceWatchState`, so a *household's* rating
   on a card is a field with no source. (`titles.community_rating` exists and
   is a different thing: IMDb's aggregate, not this household's opinion.)

4. **`Person` and `Credit` ARE built, and `Collection` with them, all three
   re-derived from `raw_payloads` with no second network call.** M4's boundary
   call 2 arriving at the milestone it named. Two providers depend on them
   (`PeopleProvider`, `FranchiseProvider`), M6 reserved search weight class B
   for exactly this, and `titles.collection_id` had been a bare nullable UUID
   with no table since M1. The payload already holds all three, so nothing is
   fetched — it ships as [03](03-sources-and-sync.md)'s fifth pipeline stage,
   `usher derive`.

   **But "the payload already holds all three" is true of the *entities* and
   not of [02](02-data-model.md)'s field lists, and the difference cost four
   fields.** `Person`'s sketch named `imdb_id`, `birth_year`, `death_year` and
   `biography`; none is on a `credits.cast[]` or `credits.crew[]` entry and all
   four live on `/person/{id}` — one request per person, which is the second
   network call this call forbids. M7 dropped all four rather than shipping a
   model whose emptiest fields imply a crawl nobody scheduled. **Unassigned**,
   and named in [03](03-sources-and-sync.md) rather than left implied.

5. **Weight class B is filled, and it needs a denormalised column on `titles`
   because a generated column cannot reach another table.** M6 promised
   *"filling B when M7 lands `Credit` is a migration rather than a rewrite"*,
   and that is true only if class B is fed from a column of the row being
   generated. A stored generated expression may reference **only the current
   row**, so `setweight(to_tsvector('english', (SELECT … FROM credits …)),
   'B')` is not expressible at all — measured on PostgreSQL 17.10, it is
   refused *syntactically*, before immutability is even considered. So M7 adds
   `titles.credit_names text[]`, maintained by the same call and the same
   transaction that writes `credits`, so the two cannot disagree.
   Still a migration rather than a rewrite of the search path, and a
   **bigger** one than M6's sentence implies: a new column, a new writer, a
   forced full rewrite of `search_document` (because
   `CREATE OR REPLACE FUNCTION` does not recompute stored generated values),
   and every fingerprint in the enriched tier moving at once. That last is
   [ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)'s scheme
   working rather than failing, at 25 s to 2 min at the measured throughput.
   Corrected in [05](05-search-and-similarity.md).

6. **`title_search_names` is still not built, and M6's own condition for
   building it is not met.** M6 deferred it to *"the day M7 lands aliases and
   people"* — and **M7 lands people but not aliases**. `alternative_titles` is
   in neither `append_to_response` list, so it is not in `raw_payloads` at all;
   landing it means changing the crawl's request shape and re-fetching the
   whole enriched tier, i.e. a metadata-provider change wearing a search
   table's name. And the people half now belongs with M9's two-tier suggest,
   which [ADR-0002](decisions/0002-postgres-first-search.md)'s failed gate
   obliges and which *replaces* the shipped suggest path — so building the
   narrow table now means M9 redesigns against a table built for the design it
   is replacing. **The condition is restated in [05](05-search-and-similarity.md)
   rather than the deferral being silently renewed**, which is the failure this
   roadmap names for the tag genome below: *an obligation recorded only where
   it was postponed is one nobody plans.*

   **M9 builds it (`m09a`), and the restatement is what made that decidable.**
   The objection above was that building it in M7 would mean M9 redesigning
   against a table built for the design it replaces; building it *in* M9,
   inside the two-tier suggest that replaces the path, is the same argument
   answered rather than deferred again. The table ships **empty** — the people
   half is M9's writer and the alias half needs `alternative_titles` in the
   crawl's request shape, which is still unassigned.

7. **The MovieLens tag genome IS built, and `genome_scores` is one dense
   `halfvec(1128)` per title rather than [02](02-data-model.md)'s implied tall
   table.** Measured on `pgvector/pgvector:pg17` (pgvector 0.8.6) at the real
   dimensions: 16,376 rows of `halfvec(1128)` is **45 MB**, while the same data
   as `(title_id, tag_id smallint, relevance real)` is 18,472,128 rows and
   **2,106 MB** — **47×**, against a database [08](08-operations.md) budgets at
   8–12 GB *total*. `real[]` sits between them at 88 MB and is worse than
   both, having no operator class. The genome is a genuinely dense matrix —
   every one of 16,376 movies carries a value for every one of 1,128 tags,
   verified by counting — so the tall form stores 16,376 copies of a tag id to
   express a matrix with no holes in it, and the dense form makes the
   similarity term a single `<=>`, the operator `SimilarityService` already
   blends. [ADR-0024](decisions/0024-the-genome-is-one-dense-vector-per-title.md).

8. **Rows build SEQUENTIALLY, and [06](06-rows-and-recommendations.md)'s
   "builds the top N concurrently" is corrected rather than implemented.**
   `AsyncSession` is explicitly not safe for concurrent use — two coroutines
   awaiting on one session interleave on one connection — so `asyncio.gather`
   over nine providers sharing the request's session is not a performance
   choice, it is a corruption. **And it usually works**, which is how it
   ships: two short reads frequently complete, and the failure is an
   intermittent `InvalidRequestError` or a result set attributed to the wrong
   query, under load, in production. The two ways out are both worse at this
   scale: a session per row means nine connections for one home screen, i.e.
   pool exhaustion at one concurrent user against a default pool; and a
   semaphore has no lane to belong to ([01](01-architecture.md)'s concurrency
   table has no lane for this, and [08](08-operations.md) already retracted
   "concurrency per lane" as a setting because *"a setting cannot be added
   ahead of the mechanism it would bound"*). Every provider's query is a
   bounded local read.

   **Pinned by a case rather than by a comment, and the case is about the
   session rather than about the clock.** The repository's usual instrument —
   measured intersection-over-union of wall-clock windows — is the *weaker*
   one here, because `asyncio.gather` over coroutines that never suspend
   produces N disjoint windows and would pass the assertion it exists to
   kill. The assertion is instead on the shared handle's in-flight depth,
   which is `AsyncSession`'s actual contract: one statement at a time. A
   second case scans `services/home.py` for `gather`/`TaskGroup`/
   `create_task`/`wait`, walking both `ast.Import` and `ast.ImportFrom` and
   matching the bare name as well as the attribute — the first case is about
   this implementation, the second about the next one.

   `usher.home.compose.duration` and the per-provider
   `usher.row.build.duration` are what turn revisiting this into a number
   rather than an argument, and `usher home` is where the number is taken.

   **The number, so the call can be acted on.** `usher home` records
   `usher.home.compose.duration` and the per-provider `usher.row.build.duration`
   breakdown, and prints both. **The sequential build is revisited when p95
   exceeds 400 ms *and* no single provider accounts for ≥ 50% of the total
   build time.** 400 ms is [ADR-0006](decisions/0006-server-composed-home.md)'s
   "instant over a slow link" apportioned — ~1 s perceived, of which ~600 ms is
   network and client render that Usher does not control; M6's 50 ms p95 is
   deliberately **not** reused, being a keystroke budget for as-you-type
   suggest, and applying it to a screen paint would condemn the sequential
   build against a bar nothing in the design promised. The second condition is
   what makes the first actionable: if one provider dominates, concurrency
   converges on that provider's latency and buys nothing, so the finding is a
   **query** to fix. Only when both hold is the redesign a session per row
   behind a bounded pool — i.e. a lane, and
   [01](01-architecture.md)'s concurrency table grows the row this call notes
   it does not have. `usher home` prints the rule beside the numbers, so it is
   read off the output rather than recomputed.

   **Measured on 2026-08-04, and neither condition fires.** `usher home
   --repeat 5` against a real `bootstrap --phase imdb` catalog of **1,271,570
   titles**, with a synthetic household seeded on top of it — 5,200 owned
   `media_items`, 360 watch states spread over two years (300 films plus 60
   episodes, so the roll-up is exercised), 50 collections of four owned members,
   200 people with 1,800 credits, and 6,000 `title_neighbors` rows over 300
   seeds. **The catalog is real; the household is synthetic and is said so** —
   a real household's watch history cannot be manufactured by a live run, and a
   measurement without one would time four providers and report the sequential
   build as free.

   | | |
   |---|---|
   | compose, cold, p50 | **23.9 ms** |
   | compose, cold, p95 | **35.9 ms** — against a 400 ms budget, **11×** under |
   | compose, warm (screen cache hit) | **0.0 ms** |
   | slowest provider | `because-you-watched`, 4.3 ms, **34%** of build time |
   | screen | 8 rows, 115 cards |

   Per provider, propose / build in ms: `because-you-watched` 3.3 / 4.3 (3
   rows), `people` 2.3 / 3.7 (2), `franchise` 2.1 / — (proposed 2, **capped
   out**), `next-up` 1.5 / 1.0, `recently-added` 0.7 / 1.7,
   `continue-watching` 0.5 / 1.8, `rediscover` 0.3 / — (proposed nothing),
   `genre-affinity` 0.0 / — (proposed 1, capped out), `seasonal` 0.0 / —
   (outside every window). **The per-family cap is visible in that table**:
   eight of ten proposals were selected, and the two that were not are the
   lowest-scoring `SOURCE` rows rather than anything a provider got wrong.

   So the sequential build stands. ⚠️ **But "not close" is a property of that
   household and was re-measured on 2026-08-05 against the scale ceiling** — a
   synthetic population owning all 1,277,878 items with 1,086,149 played —
   where compose is **p50 710.3 ms, p95 783.4 ms**, i.e. 2× *over* the budget.
   The call is unchanged and the reason is the second condition: `genre-affinity`
   is **98%** of build time there, so the answer is to fix one provider rather
   than to run nine concurrently on one session. Read the 11× as scoped to
   5,200 owned copies.
   [ADR-0025](decisions/0025-rows-build-sequentially.md) records it, because
   the way this ships wrong is that concurrent *usually works*.

9. **Row provider enable/disable does not become a table.**
   [08](08-operations.md) puts it in the *database* layer — *"Sources, users,
   row provider enable/disable | Runtime, via admin API"* — and the admin API
   is M9's. A table whose only writer is a route in a later milestone is the
   `search_queries` failure again, and this one would be worse: a
   `row_providers` table with **ten** rows all reading `enabled = true` is
   indistinguishable from no table, right up until an operator finds it and
   expects toggling it to do something. **Providers are enabled by
   registration in code in M7**, and [08](08-operations.md)'s row is annotated
   with its owner rather than left implying a control that exists. It is also
   what makes the injected clock belong on `RowContext` rather than on each
   provider's constructor: a provider registered once cannot take a
   per-request argument at construction.

   *(This item said **nine**, which was true when it was written and stopped
   being true with `CuratedProvider`: `row_providers()` returns ten. Corrected
   rather than left as a stale counted fact.)*

   **Come due in M9 (`m09a`), and the refusal's own condition is what
   discharges it**: `row_provider_settings(slug_prefix PK, enabled, updated_at)`
   lands with the admin API that writes it. The half of the refusal that
   survives is that it is **created empty** — an absent row means enabled,
   which is exactly what *"enabled by registration in code"* already means, so
   there is no state where the table exists and says nothing. It is
   deliberately **not seeded with ten slugs**: a migration hard-coding the
   registry is a second copy of `services/rows/__init__.py` with nothing
   anywhere to detect drift. The natural key is `RowProvider.slug_prefix`,
   which that port already declares *"declared rather than derived"* and
   *"bounded at ten"*.

   ✅ **Discharged.** The table landed with `m09a`, the writer
   (`RowProviderSettingsRepository`) with M9's E1, and the routes that give
   toggling it a meaning — `GET`/`PUT /admin/rows/providers`
   ([07](07-client-api.md)) — with E2, in the same commit as the read that
   makes the refusal's own condition false: a disabled provider is filtered out
   of `GET /home`, of `usher home` and of the background screen refresh, so an
   operator who finds this table and toggles it watches a shelf disappear. The
   filter is *filtering*, not *enumeration* — no composition root names a
   provider — so this item's other half, *"a list a composition root assembles
   by hand is a list the tenth provider is forgotten from"*, is not reopened.

**M7 is complete, and it is the first milestone whose *subject document* was
written entirely before any of it existed.** [06](06-rows-and-recommendations.md)
carried no `⏳` and no `🔶` anywhere: every statement in it read as shipped, and
five were not. It described a concurrency model that would corrupt a session
(call 8), a `RowCard` field with no table behind it (call 3), a taste model
built on a rating column this system has never had, a caching row — *"neighbour
tables: rebuilt on embedding change"* — describing a trigger that has never
existed, and a taste-centroid invalidation that would have been a million
messages a night. **A document with no markers is not a document with no gaps;
it is a document nobody has audited.** All five are corrected in place, and
each says what happened to the sentence it replaces.

**Two obligations that were M7's and belonged to nobody. One now has an owner
and a mechanism; the other is restated as unassigned with its reason.**

- **`alternative_titles` is answered, and not by TMDb.** It stayed unassigned
  because it needs an `append_to_response` change plus a re-crawl of the whole
  enriched tier — and that is still true of *TMDb's* `alternative_titles`,
  which nothing plans. What M9 shipped instead is **IMDb's `title.akas` as the
  alias source**, through `usher bootstrap --phase aliases`
  ([04](04-catalog-bootstrap.md)'s Phase 0b): no API call, no change to the
  crawl's request shape, and it fills the `alias` half of
  `title_search_names` for **399,046 of 1,271,138 titles (31.4%)** with
  **1,663,364** rows carrying a `region` and a `language`. So call 6's other
  half is discharged by a different source, and the blocker it was waiting on
  is no longer on anyone's critical path. TMDb's own alternative titles remain
  unassigned and are now a *duplicate* of a capability rather than the only
  route to one.
- **`Person`'s four `/person/{id}` fields (call 4) stay unassigned, and the
  reason has hardened.** They are `imdb_id`, `birth_year`, `death_year` and
  `biography`, none of which is on a `credits.cast[]`/`crew[]` entry, so
  filling them is one request per person over ~200k people. The obvious
  cheaper route — take them from IMDb's `name.basics`, which carries a birth
  and death year for 15.5M people — **does not exist**: a TMDb credit entry
  carries no `nconst` and IMDb carries no TMDb id, so the only merge key the
  two sources share for a person is the name, and by
  [ADR-0003](decisions/0003-own-uuid-identity.md) a name is not identity. M9
  reads both files and deliberately writes **no person row** from them (see
  below), so nothing here changed except that the alternative is now measured
  rather than assumed.

**What M9's IMDb expansion deliberately did not build.** `titles.credit_names`
is filled from `title.principals` × `name.basics` with **no `people` and no
`credits` row written from IMDb at all** — the entity design was measured at
**2.702 GB against a 2.0 GB ceiling** and refused, so the two bulk sources
never own one entity and the provenance question that design raised does not
arise. There is no new table and no new migration for either phase: aliases
land in `m09a`'s `title_search_names` and credit names in a `text[]` column
M7 already added. `title.crew` and `title.episode` are still not imported and
still have nowhere to land. Full evidence, including the bar written before
the download, is in `.claude/rules/bootstrap-and-datasets.md`.

**M8's boundary was ambiguous in eight places, and each was decided
deliberately rather than drifted into.** The plan states each with its reason
and this is the roadmap's copy, which is where a reader looking for *what a
milestone declined* is told to look.

1. **`LiteLLMClient` is not built.** The client is an OpenAI-compatible HTTP
   client over the httpx stack this project already has. Three PRD sections
   had named litellm since M1 — [01](01-architecture.md)'s ports and stack
   tables, [06](06-rows-and-recommendations.md)'s step 2, and
   [10](10-telemetry-and-dashboards.md)'s *"litellm reports per-call cost
   natively"* — and all three are **corrected rather than implemented**.
   Priced rather than argued: **+146 MB and 29 distributions against +0 and
   0**, on a 356 MB image, and the 29 are a second async HTTP stack plus a
   model-download client and two tokenizer runtimes — the exact shape
   [ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)
   refused for the embedder. **The deciding fact is not the size: `base_url`
   already *is* the provider abstraction.** What litellm adds over it is
   Anthropic's and Gemini's native wire formats, both reachable through
   OpenRouter, which speaks this one.
   [ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md).
2. **Generation is a job, never on a request path**, and
   `POST /admin/rows/regenerate` **enqueues and returns 202 with the job's
   key**. [06](06-rows-and-recommendations.md) already required it; what the
   boundary adds is *why the route is not synchronous*, and it is structural
   rather than a latency preference: a synchronous route is the first request
   in this project whose honest answer is *"the upstream is down"*, which
   would force [07](07-client-api.md)'s RFC 9457 envelope a milestone early
   against a deferral that has now survived three client-facing routes. Enqueued,
   the only remaining failure is "the queue is unreachable", which is Postgres,
   which is already a total outage.
3. **The prompt addresses candidates by a small integer index, and the service
   owns the index→UUID map.** Measured over one real 200-film pool: a UUID
   handle costs **3.1× the prompt tokens and 3.2× the latency** of an integer
   index and is the **least accurate of the three** spellings tried. The
   argument is not the rate, which is one model on one evening — it is that an
   index is **bounds-checked**, so an out-of-pool identifier is
   *unrepresentable* rather than merely rejected, where a hallucinated UUID
   denotes nothing and a hallucinated IMDb id may denote a real film the
   household does not own.
   [ADR-0028](decisions/0028-the-pool-is-the-contract.md).
4. **Validation coerces before it compares, and counts its drop reasons
   separately.** **108 of 108** identifiers came back as JSON integers where a
   probe schema asked for strings, with a hallucination rate of **zero**; a
   validator holding a `set[str]` drops every row, writes nothing, logs
   nothing, and is indistinguishable from a model that had nothing to say.
   **Shipped as five reasons rather than two** (two counting rows, three
   counting cards), and 🔴 **the coercion turned out to be the *primary* path
   rather than a fallback** — the shipped schema asks for `integer` and the
   validator keys on `str(index)`, so it runs on 100% of cards on the happy
   path. ADR-0028 carries both corrections.
5. **The candidate pool degrades without an embedder, and the taste centroid
   is an additional signal rather than the pool's spine.**
   [06](06-rows-and-recommendations.md) said the pool is *"pre-filtered by
   taste-centroid proximity"*, and `USHER_EMBEDDING_ENABLED` defaults to
   `False` — so implementing that literally makes curation the feature that
   never fires on the shipped configuration, **exactly the failure that
   document already corrected once** for `GenreAffinityProvider`. The pool is
   built from signals needing no model and the centroid **re-ranks**. This is
   also what finally gives `TasteService.centroid` a caller in `src/`, the gap
   M7 shipped and named.
6. **`llm_calls` ships with a writer for every column, and
   [02](02-data-model.md) gets the row it never had.**
   [10](10-telemetry-and-dashboards.md) specified the table in a ten-column
   SQL comment that PRD 02's supporting-tables list did not mention; both are
   fixed. The `search_queries` discipline cuts *for* this table — M8 owns the
   writer and the table together — and it binds every column: a column M8
   could not fill did not ship. **There is no `user_id` on `llm_calls`**,
   deliberately: it is a cost ledger, and spend is attributed to an outcome by
   joining `curated_rows` on `generation_id`.
7. **No `usher.llm.*` metric is added, because [10](10-telemetry-and-dashboards.md)
   says spend is SQL.** Two metrics are added and neither is about money —
   `usher.curation.rows` and `usher.curation.dropped` — because they answer the
   question no `llm_calls` row can: *whether the validator is eating the
   output*. A call that returned 200 and produced nothing usable is `ok = true`
   from the wire's side.
8. **Nothing schedules the nightly run, and the plan says so rather than
   inventing a scheduler.** [06](06-rows-and-recommendations.md) says
   generation *"runs nightly and on demand"*; there is no scheduler anywhere in
   `src/` — `api/lanes.py` is one lane per source plus one worker — and every
   other periodic thing here is an operator's cron entry, which
   [10](10-telemetry-and-dashboards.md) already calls out for
   `usher similar --rebuild`. `usher curate` is the command and the README
   carries the cron line. Building a scheduler for one job would be a second
   unplanned milestone inside this one, and [08](08-operations.md)'s
   mechanism-before-the-setting rule forbids the alternative of a
   `USHER_CURATION_SCHEDULE` with nothing reading it.

**M8 is complete, and the thing it got wrong is not in the list above.** Every
boundary call held. What the live verification found on 2026-08-07 — against a
local `gemma-4-26b-a4b` over a real 1,271,138-title catalog, 36 completions of
a 45-completion bound — is that the **product** claim underneath the milestone
is the weak one: **52 of 59 generated headings (88%) were genre labels**, which
the prompt explicitly forbids, and one heading in 59 named a filmmaker. On that
model the curated shelf is substantively what `GenreAffinityProvider` already
produces from a `SELECT`, for free. ⚠️ One model, one evening, and the
percentage transfers to nothing; **what transfers is that the prompt's grouping
instruction is not self-enforcing and nothing in this system checks it**, which
is a property of the design. Recorded in
[06](06-rows-and-recommendations.md) as a known limit rather than fixed, because
curated rows are additive and [08](08-operations.md)'s *"Home composes without
them"* is what makes a dull row a disappointment rather than a defect. Three
further limits are recorded there with it — the pool's missing ownership
*filter* against a prompt that says *"own library"*, cross-row de-duplication
resting on a prompt rule, and `min_cards = 5` giving a small unwatched pool zero
rows at full price — and 🔴 `USHER_CURATION_POOL_SIZE`'s `le=1000` is a bound
the reference endpoint cannot serve (measured: 600 works, 700 and 1,000 both
return HTTP 400), because nothing couples it to `USHER_LLM_MAX_OUTPUT_TOKENS`.

### M9's boundary calls

**Eight things M9 deliberately does not build**, each with its reason, recorded
here and in `.claude/rules/milestone-boundary-calls.md` and nowhere else — this
milestone's own plan states them once and every task that touched one points
back here rather than restating it.

1. **Authentication.** `current_user` keeps returning the singleton default
   user and [01](01-architecture.md)'s seam is still filled by replacing one
   dependency. Designing authorization against routes that land in the same
   milestone is the mistake [07](07-client-api.md) avoided four times with the
   error envelope — and M9 is where that particular deferral is finally cashed,
   by designing the envelope against routes that already exist.
2. **The GIN → GiST swap for tier-2 suggest.** The 2.8-point recall gain is
   measured against synthetically mutated real titles, not against anything a
   person typed; `search_queries` is the evidence that would settle it, M9
   builds that table, and it has no rows until after M9 ships. The two indexes
   also cannot coexist — a GiST trigram index beside the GIN one makes the
   planner take GiST for `%` and costs the shipped path **4.3× on p50 for
   identical recall**. Deferred, not rejected, and
   [ADR-0031](decisions/0031-the-two-tier-suggest.md) says which.
3. **Meilisearch.** Unchanged from M6, and still a *justified* candidate rather
   than a hypothetical one — see *Post-v1 candidates* below.
4. **Byte proxying for playback.** The ticket is a `302`; the client fetches the
   target directly. **Images *are* proxied**, and the distinction is the
   subsystem rather than an inconsistency: an image is small, cacheable and
   reusable across households, a video stream is none of the three.
5. **Per-client scoped tokens** — [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md)'s
   option 2. It needs a client identity that does not exist until
   authentication does, which is call 1.
6. **A scheduler.** The write-back retry rides the existing Postgres job queue
   with `run_after`. Building a scheduler for one retry would be a second
   unplanned milestone inside this one, exactly as M8 argued for the nightly
   curation run — and every periodic thing in this project is still an
   operator's cron line.
7. **Query expansion stays off by default.** M8 measured it *worse* and M9 does
   not re-litigate a measurement by flipping a default. What would settle it is
   the carried-debt entry below: a real evaluation set out of `search_queries`.
8. **The 45 columns that leak a raw driver exception.** 31 of the 45 are
   written through `copy_records_to_table` on the raw asyncpg connection, where
   an out-of-range int raises a bare `OverflowError` with **no SQLSTATE** — so
   there is nothing to map to a problem `code` without wrapping the bulk path
   itself, and no widening of `except IntegrityError` reaches them. Left in the
   carried-debt list below with that reason attached.

✅ **The one thing M9 shipped as a gap — the live Emby verification — ran on
2026-08-12, after the gate, and both halves passed.** H4
(`POST /titles/{id}/play` → a minted ticket → `302` → a real `206` from the
source) and H5 (the watch write-back round trip, read back **from Emby**) drove
the shipped surface over real sockets against the operator's real Emby 4.9.5.0
in **23 bounded requests with no walk of any kind**. The evidence is in
`.claude/rules/emby-push-and-ingest.md`, per the convention that live-run
evidence lives with the subsystem it measures.

🔴 **What this section said until then was wrong, and the way it was wrong is
worth more than the runs.** It read *"no Emby credentials exist on this host —
verified rather than assumed"*, and what had been verified was `~/code/usher/.env`
and nothing else. The operator's Emby base URL, token, user id and device id
were in a Home Assistant secrets file one directory over — which is exactly the
*"operator's own secrets file"* that `CLAUDE.md`'s live-verification rule tells
such a run to read. **A negative established by checking the one place the
answer was expected is not a negative**, and this one cost a milestone the two
runs whose entire product is live evidence. The claim was repeated in **eight
places across seven files**; the reconciliation that was supposed to keep them
in step counted five.

What the runs settled, briefly — the full ledger is in the rules file:

- **The read half is bytes, not a redirect.** The `302`'s `Location` is
  byte-for-byte the URL `build_stream_targets` builds, and a `Range` request
  against it answers `206` with `video/x-matroska` content. The **ticket path
  mangles nothing**, and the specific candidate — double percent-encoding of the
  `deep_link` wrapper — does not fire.
- **The leak claim has a control.** The token is found in the `302`'s
  `Location`, where it must be, *before* its absence from the `/play` body is
  believed.
- **Ticket expiry was driven live rather than named as unverified**: honoured
  at 127 s, refused at 312 s. Group D shipped the TTL as a constant and
  deliberately not as a setting, so it was held against the wall clock instead.
- **One thing H5 was specifically meant to observe is no longer a standing 🔴
  risk** — the `POST /PlayedItems` position-clearing divergence on D8's
  write-back handler is now measured, in the direction M3 predicted, through the
  shipped route and the shipped job.
- **The write to a real account was restored byte-for-byte**, the before/after
  diff empty, on an item chosen precisely because its `UserData` was all-zero.
- **H2's conformance pin, H6's reconciliation and H7's gate are unaffected.**
  None of the three ever depended on H4 or H5 landing, only on their disposition
  being honest — and that disposition is what changed.

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

✅ **Discharged on 2026-08-12 by M9, and closed here rather than deleted** —
this subsection is the record of an obligation that survived three milestones,
and the useful thing about it is that it was written down before anyone knew
what it would cost. [ADR-0031](decisions/0031-the-two-tier-suggest.md) is the
decision: **one route with `?tier=`** rather than two, a `SuggestTier` enum
reaching `/openapi.json` and defaulting to `prefix`, both indexes as required
collaborators of `SearchService`, and a minimum prefix length per tier. M9's B2
built tier 1, B3 measured it at catalog scale, B5 put both on the wire.

**Two of B3's four pre-registered bars failed, and the ADR says so at the top
rather than at the bottom.** Both failures are attributed away from the shipped
code by *measurement* rather than by argument, and the bar that passed does not
cover the defect the ADR is mostly about. Two consequences for a later reader:
**bullet 1's "p50 0.6 ms" is a per-query figure and not a per-request one** —
B3's own `SELECT 1` through the same path measures p50 0.557 ms, so the
overhead floor is the size of the measurement — and **the union at a
one-character prefix is 2,707 ms p95**, which is what the minimum prefix length
exists for and is the number ADR-0031 narrows ADR-0002's *"the only thing
measured that fits inside a keystroke"* against.

**Boundary call 2 above is what is left of this subsection.** The GIN → GiST
choice bullet 2 describes is *not* taken, for the reason stated there, and the
evidence that would settle it is bullet 3's — `search_queries` now exists and
still has no rows.

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
`SimilarityService`'s existing signal list.

**Shipped in M7, and the cost line above was wrong in two places — corrected
here rather than quoted.**

- **"One weighted term" is one entry and one accessor *in the scorer*, plus two
  port DTO fields, two widened statements, both fakes, and the port's
  abstract-method pin.** `NeighborSeed`/`NeighborCandidate` live in
  `ports/repository.py`, so a fourth signal is a **port** change — which is a
  fake change and a surface-pin change by construction. `_blend` itself is
  untouched, so the *spirit* of the estimate held; its blast radius did not.
- **The list omitted invalidation entirely, which turned out to be the larger
  item.** Adding the term re-weights the other three, so every row already in
  `title_neighbors` was computed under a different meaning — and nothing
  distinguished the halves. M7 therefore also ships
  `title_neighbors.blend_fingerprint` (migration `ffb`, the milestone's fifth
  against a plan budgeting four, named in advance rather than discovered),
  `usher.similarity.neighbors.stale`, and a `usher similar` that says so per
  title. **A full `usher similar --rebuild` is required after upgrading, and
  nothing schedules it.**

That fingerprint closes the *meaning-changed* half of staleness and explicitly
**not** the *some-other-title-was-embedded* half, which remains undecidable per
row exactly as M6 argued. M6's age-not-fingerprint reasoning stands and is not
reversed.

**Settled in M7 — what was built, from which archive, and what it covers.**
Retired in place rather than deleted, because a reader who remembers the
paragraph above needs to find out what happened to it.

- **The archive is `ml-latest.zip`, and the choice is forced rather than
  preferred.** `ml-32m` (05/2024) is the newest full release and **dropped the
  genome entirely** — four members only. `ml-25m` still has one and is the only
  genome-bearing archive whose licence says *"the user may not redistribute the
  data without separate permission."* `ml-latest` has a genome **and** the
  permissive clause. All three are recorded in
  [04](04-catalog-bootstrap.md)'s licence section, because the reason the
  licence row is right is only legible next to the two archives it is not.
- **Three of that document's numbers were wrong and are corrected with their
  measurement**: 18,472,128 relevance scores rather than "15.6M", 334.6 MiB
  rather than "250 MiB" (which is `ml-25m`'s size — the right number for the
  wrong archive, which is why it survived review), and 16,376 movies rather
  than 13,816. The 1,128 tags was exactly right.
- **The coverage promise finally has denominators, and the one that matters is
  not the one anyone quoted.** [05](05-search-and-similarity.md)'s *"~7%"* was
  roughly right about the ≥100-vote priority tier (**7.61%** measured) and
  wrong about everything else: **1.22%** of all titles, **10.68%** of a real
  household's **5,020**-title owned library. The figure that decides whether
  the term does anything is the **candidate-pair** rate — both sides of a pair
  need a vector — measured at **1.81%** over those same 5,020 seeds, never
  squared. That is **below the 10% floor the weight assumes**, so the term
  ships at 0.25 with the pool-vs-revert choice deferred to M9 and the reason
  recorded rather than the number quietly absorbed. **The seed count travels
  with the rate**, because 1.81% is a floor over one household's owned,
  name-shaped titles and is not a baseline any differently-selected population
  can be compared against.

  ✅ **The deferred choice was made on 2026-08-12 by M9's S7, and it is the
  revert.** M9's S1 first established that those 5,020 seeds sat in a database
  that no longer exists and had been promoted by a tier-label `UPDATE` that
  moved no document, so the pool was drawn **by name** — 1.81% is not a
  baseline for anything. M9's S5 then re-measured over the enriched tier, one
  pool walk, and got **2.4746%** (323,297 of 13,064,700 candidate pairs, 130,647
  seeds, 15,525 carrying a vector at 11.883% single-side). **A second
  measurement, never a rise** — both numbers stay in the record with their
  populations attached — and it is still **four times below the 10% floor**. So
  `_WEIGHTS["tags"]` and the `tags=` argument come out together (never a 0.0
  weight, which is arithmetically identical to absence while still moving
  `blend_fingerprint`), `cosine`/`keywords`/`genres` stay at M7's
  0.45/0.20/0.10 rather than returning to M6's, and the genome-aware **pool**
  option is not taken — M9 changes no statement and no `_CANDIDATE_POOL`. The
  vectors, the pair read and `usher similar --rebuild`'s coverage counters all
  stay, so the number this would be re-opened on keeps being reported.
  [ADR-0024](decisions/0024-the-genome-is-one-dense-vector-per-title.md) carries
  the amendment, the ±0.0167 residual and the ceiling no enrichment can move
  (`ml-latest` is movies-only, frozen 2023-07-20, and scores 16,376 of its own
  86,537 movies). ✅ **The rebuild this obliges was run by H7 on 2026-08-12** —
  88.3 minutes over 130,647 seeds, `stale_neighbors()` **0** against
  **3,266,175 rows**, every one stamped `78f3ecd20e654c0f6aa4bdf646ec099b`. The
  row count is recorded beside the verdict because an empty table reports no
  stale rows either, and empty is what `title_neighbors` held on every catalog
  on this host until that run. The rebuild's own
  `323,297 / 13,064,700 = 2.4746%` reproduced S5's walk to the integer, which
  is the control that keeps the figure above meaning something: the pool is
  invariant to a weight change, so a disagreement would have voided it.
- **And "one weighted term in `SimilarityService`'s existing signal list" was
  wrong in the two ways above**, which is why the cost line is corrected in
  place rather than quoted approvingly.

**M9 owes ADR-0012 a successor.** In v1, `POST /titles/{id}/play` returns a
target URL carrying the source's session token, because M3 has no HTTP surface
to redirect from. M9 builds that surface, so M9 is where the opaque,
short-lived playback ticket lands — a `302` to the real URL, which makes the
shareable artifact opaque rather than removing the grant. Listed here and not
only in the ADR that defers it: an obligation recorded only where it was
postponed is one nobody plans.

## Carried debt — found by a milestone, owned by none

By the same rule as the paragraph above. Each of these was **measured** during a
milestone that did not own it, recorded where it was found (a rules file, a PRD
section, a module docstring), and left without a milestone. Recorded here so the
roadmap says who owes them, because a finding filed only next to the code it
concerns is one nobody schedules. Every entry names its evidence; none is a
suspicion.

- **51 columns leak a raw driver exception across the port boundary** — found by
  M8, measured live against Postgres 17.10 / asyncpg 0.31.0 with every mechanism
  driven through the real repository method. ✅ **Scoped by M10's F8 on
  2026-08-20 —
  [ADR-0041](decisions/0041-a-bounded-column-is-a-declared-type-that-refuses.md)
  states the bounding rule, publishes the per-column ledger these figures never
  had, and hands F9 a bounded set of 22.** Every number below is re-measured at
  `m09f` by `scripts/audit_bounded_columns.py`, which derives the ledger from the
  SQLAlchemy metadata and cross-checks it against an independent replay of the
  migration chain, offline and with no database. 🔴 **The headline correction:
  of the five figures this bullet has carried since M8, only two reproduce.**
  `67` and `5` are right and regenerate under every reading of the rule; `17`,
  `45` and `31` do not, and `17` never could have, because no ledger of it was
  ever published to compare a membership against.

  | claimed at `m08b`, quoted since | verdict, measured 2026-08-20 at `m09f` |
  |---|---|
  | **67 bounded columns** | ✅ **Right when written, and the one genuine reproduction here.** `--at m08b` prints 22 `varchar(N)` + 44 `integer` + 1 `numeric(12,8)` = **67**. At `m09f` that same rule gives **75** (M9 added `images.kind/width/height`, `search_queries.mode/result_count/latency_ms`, `title_search_names.kind`, `credits.source`); ADR-0041's rule adds the one `bigint` and the three `halfvec(N)` for **79**, and there are 6 further CHECK-only value bounds it deliberately excludes |
  | **5 already translated** | ✅ **Right, and exact.** `curated_rows.position` and the four `llm_calls` columns, at `m08b`, under every reading. Now **10**, since M9 gave `images` and `search_queries` the same treatment |
  | **17 provably safe** | 🔴 **Not reproducible under any reading, and never scoreable.** ADR-0041 publishes three readings of what closes a value set; at `m08b` they give **18 / 16 / 12** and seventeen is none of them. Two columns the plan counted as safe are not: `titles.imdb_id`'s `pattern` is on `domain.Title` while the bulk writer takes `ports.bulk.ImdbTitle` (a bare `str`), and **`jobs.priority`'s `Field(ge=0, le=100)` is on `domain.Job` while `enqueue` takes `JobRequest`, whose `priority` is a bare `int`** (`ports/jobs.py:45`). A domain bound the write path never runs is not a bound |
  | **45 exposed** | 🔴 **Not reproducible.** It is `67 − 17 − 5` and 17 is not reachable; the same subtraction with the adopted reading's 16 gives 46, and Rule B's own exposed figure at `m08b` is **50**, at `m09f` **51** — 31 at the COPY, 20 at a SQLAlchemy statement |
  | **31 through the COPY path** | 🔴 **Not reproducible as stated, and the number recurring is a trap rather than a confirmation.** The adopted reading does put 31 in the COPY bucket — `37` narrow-staged bounded destination columns minus `6` provably safe among them — but the other two readings give **30** and **34**, and `37 − 7 = 30` is the plan's own stated alternative. A figure that appears under one reading of three is not a reproduction, and the membership is not the old 31 in any case: **28 raise `OverflowError` and 3 do not.** `media_items.container`/`video_codec`/`audio_codec` are refused **server-side during the COPY** as `StringDataRightTruncationError`, SQLSTATE **`22001`** — a real SQLSTATE on an exception that is still not a `DBAPIError`. **There are two failure shapes, and a fix that widens `bigint` and forgets `text` reaches 28 of 31** |

  **M9's boundary call 8 survives all of it, and ADR-0041 does not re-open it**:
  the COPY-bucket writes go through `stage_records`' `copy_records_to_table` on the raw
  asyncpg connection, outside SQLAlchemy's error translation entirely, so
  `is_row_refusal()` cannot inspect either shape and **no widening of `except
  IntegrityError` can reach them**. `_errors.py`'s scope claim (class 22 + class
  23 covers "not storable as given") is true for SQLAlchemy-executed statements
  and does not model the COPY path. 🔴 **What ADR-0041 does retire is the
  candidate fix this bullet named**: *"declare staging columns wide (`bigint`,
  `text`) so refusal moves to the `INSERT … SELECT` where the existing net
  catches it, evidenced by `id_crosswalk.imdb_id`"* — measured at HEAD, **on
  that path there is no net**. `stg_crosswalk.imdb_id` is already `text`, the
  refusal already lands on the `INSERT … SELECT`, and `bulk.py:upsert_crosswalk`
  has no `except` at all. The same holds at the three other places the pattern
  already exists — `stg_titles.imdb_id text` → `titles.imdb_id`,
  `stg_genome.relevance real[]` → `genome_scores.relevance`, and
  `stg_title_embeddings.embedding text` → `title_embeddings.embedding`.
  **Four for four: the candidate is two changes, not one**, and the second one —
  which destination `except` a staged `INSERT … SELECT` with a `CAST` may safely
  take, given `_errors.py:66–75`'s own caveat that class 22 is about the row only
  for a parameterised statement with no server-side expression — is the
  bulk-loader design task M10 does not own.
  [ADR-0030](decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)
  closed the problem-code vocabulary at seven members on the evidence of routes
  that exist, and none of this needs an eighth.
  ⚠️ **`bulk.py` does translate, twice** — `refusals_as_conflict` at `:483` and
  `:778` — so its seven untranslated writers are an omission at each site rather
  than a module that never learned the mechanism. ADR-0041's first draft said
  the module contained no `try`/`except` at all and drew an inference from it;
  that was false and is corrected there.

  ✅ **What M10 owned was F9's 22, and F9 landed it on 2026-08-20 — 21 of the
  22, with the twenty-second excluded on evidence.** Twenty writing sites took
  `refusals_as_conflict` or the widened `except DBAPIError` + `is_row_refusal`
  (eleven replacing an `except IntegrityError`, nine where there was no `except`
  at all — counted by an AST walk, not by listing them), and `stg_genome.tmdb_id` and `stg_akas.ordering` widened to `bigint`. No
  migration: the only DDL touched is two `CREATE TEMP TABLE` strings in
  `bulk.py`. The `exposed-sqlalchemy` bucket went **20 → 1**.
  **The one left is `jobs.attempts`, and translating it would have been the
  misuse rather than the fix**: its only writer computes it server-side as
  `attempts = attempts + 1`, so a class-22 refusal there is a *statement* fault,
  and reporting one to a caller as its row being wrong is exactly what
  `_errors.py:66–75` warns against. `JobRequest` carries no `attempts` field, so
  no port call can supply a value for it either. ADR-0041's *"What F9 did"*
  section carries the per-site reasoning, including why the two `CAST`-carrying
  destination statements the record predicted would fail question (3) in fact
  pass it. The other nine staging-only integers are `enumerate()` ordinals and
  are bounded by the batch's own length.

  ✅ **And `22001` on the COPY path is now observed rather than asserted.**
  ADR-0041 flagged it as the one failure shape it took from the protocol and
  never ran; measured through the shipped `stage_records` on
  `pgvector/pgvector:pg17`, it is exactly what that record predicted —
  `asyncpg.exceptions.StringDataRightTruncationError`, `sqlstate == "22001"`,
  **not** a `sqlalchemy.exc.DBAPIError` and carrying no `.orig` chain, so
  `is_row_refusal()` cannot be handed it. **M9's boundary call 8 therefore now
  rests on a measurement.** `tests/integration/test_staging.py`.

  ✅ **Separately, and closed by F9 on 2026-08-20:** `Title.popularity: float |
  None = Field(ge=0)` accepted **infinity** — `float('inf') >= 0` is `True`,
  Postgres 17 stores it, verified round-trip, reachable via `json.loads('1e400')`
  from a TMDb payload. It now carries `allow_inf_nan=False`, and
  `adapters/tmdb/mapping.py:_non_negative_float` filters non-finite values to
  `None` **in the same commit** — that module's contract is that nothing TMDb can
  put in a payload may raise, and a `pydantic.ValidationError` is not a
  `UsherPortError`, so an unfiltered `inf` would have escaped `EnrichService`'s
  `except` and killed the worker instead of parking the job. The bound is on the
  model rather than on the column because there is no width to widen: see below.
  `community_rating` is safe only by accident of its `le=10`, which is now a case
  (`test_community_rating_refuses_a_non_finite_value_by_its_ceiling`) so that
  relaxing the ceiling cannot quietly re-open it.
  ⚠️ **`titles.year` and `titles.vote_count` are the same `Field(ge=0)`-against-
  `integer` shape and are deliberately *not* closed**, with
  `test_year_and_vote_count_still_accept_a_value_their_column_cannot_hold`
  recording the exclusion rather than leaving it unstated. Both are in the
  `exposed-copy` bucket, and a ceiling on `Title` would be invisible to the only
  writers that overflow them — `bulk.py:upsert_titles` and
  `bulk.py:apply_ratings` take `ports.bulk` frozen dataclasses and never
  construct a `Title` at all (ADR-0041, question 5).
  ⚠️ **This bullet named "Postgres 17's unbounded `NUMERIC`" as the column until
  2026-08-20, and that column is not `NUMERIC`** — `titles.popularity` is
  `sa.Float()` (`a8a0e10ff464:102`, mirrored in `db/models/title.py`), which
  PostgreSQL resolves to `double precision`. The round-trip observation stands;
  the mechanism named for it did not. That is also why ADR-0041's rule excludes
  it: a `float8` refuses nothing a Python `float` can hold, so this is an
  *unbounded* column accepting a nonsense value — the opposite defect from the 50
  above, wanting a domain bound rather than a translation. The same correction is
  owed to issue #10's body, which carries the `NUMERIC` spelling.
- **`PortRateLimited.retry_after` reaches no consumer** — an M4 gap found by M8.
  **Six sites across four adapter modules** construct it — `adapters/bulk/
  wikidata.py`, `adapters/bulk/download.py`, `adapters/emby/session.py` (three)
  and `adapters/http.py` — and `git grep retry_after src/`
  finds **zero** consumers.
  ⚠️ *This bullet read "seven raise sites across five modules" until 2026-08-19,
  and both halves were wrong. Re-measured by an `ast` walk for
  `PortRateLimited(...)` calls under `src/`: **six across four**, of which
  **five are `raise` and one is a `return`** — `adapters/http.py`'s
  `port_error_for` hands the error back for its caller to raise, so "six raise
  sites" is a shade off in the other direction. M9's D9 plan had already
  measured the same six ("an earlier draft said 'seven sites across five
  modules'; measured, it is six across four") and this bullet kept the draft;
  `db/repositories/jobs.py`'s module docstring, PRD 08 and
  `.claude/rules/emby-push-and-ingest.md` all state the corrected census.*
  `JobWorker._fail` passes only `retryable=True`, and
  the backoff is computed from attempt count alone, so a 429 telling us exactly
  when to return is answered with a jittered guess and the hint survives only as
  prose in `jobs.last_error`. Affects every job kind. **M9-sized** — it needs a
  `run_after` argument on `JobQueue.fail` and a change to `_FAIL`'s `CASE`, which
  is a port every kind shares.
  ✅ **Paid by M9's D9 on 2026-08-11**, exactly as sized: a keyword-only
  `retry_after_seconds` on `JobQueue.fail`, `GREATEST(:retry_after_seconds, 0)`
  inside `_FAIL`'s `make_interval`, and `JobWorker._fail` reading the hint
  through `isinstance(exc, PortRateLimited)` rather than a `getattr`. The grep
  in this entry now finds **one** consumer, which is what closes it.
  **Two things the closing measured that this entry did not predict.** The plan
  argued that binding a raw `None` would fail on asyncpg as an untyped
  parameter; it does not — `GREATEST(…, 0)`'s sibling literal resolves the type
  and `GREATEST(NULL, 0)` is `0` — so the Python-side normalisation is kept for
  a *different* reason (the floor's correctness should not depend on a literal
  staying textually adjacent) and the docstring saying otherwise was corrected.
  And the pre-existing `test_backoff_is_jittered`, carried since M4, asserted
  `len(distinct) > 1` over twenty round trips, which **real `clock_timestamp()`
  drift satisfies on its own** (~8 ms measured with the jitter term deleted,
  against ~410–440 ms with it): both that case and D9's new one now assert a
  *magnitude*.
  ⚠️ **This entry stays in carried debt, and what it owes is now stated as two
  claims rather than one** — because the ✅ above invites the reader to take
  both, and only the first is bought. **The mechanism is pinned; the upstream
  behaviour is not.** Since 2026-08-19 (M10's S4) the chain is exercised end to
  end by `tests/integration/test_rate_limited_end_to_end.py`, which fails a real
  `match` job through the shipped `EmbyAdapter` and the shipped `JobWorker`
  against real Postgres and reads `run_after - clock_timestamp()` back out of
  the row — over **both** of RFC 9110's `Retry-After` forms, an integer and an
  HTTP-date, the second being the one `float(value)` alone raises `ValueError`
  on and which carried that bug in two separate copies before
  `usher.adapters.http.retry_after_seconds` was shared. Measured: **147.0 s**
  and **141.4 s** on the two hinted arms against **15.5 s** and **26.1 s** for
  the same job under a 429 carrying no header at all, i.e. the ordinary jittered
  [15, 30) draw. So the field is pinned by two cases and by real interval
  arithmetic rather than by one case and a Python transcription of it.
  ⚠️ **The second claim is the one nothing can buy here: no upstream this
  project talks to has ever sent a 429.** M9's T2 saw none in **393** TMDb
  requests, with no `retry-after` on its one 400; M9's S3 saw none in
  **130,334** TMDb requests, with no `Retry-After` on any of its 193 non-200s;
  M9's H4/H5 saw none in **23** requests to a real Emby 4.9.5.0, with
  `run_after` NULL on the only queued row. S4's 429 therefore comes from a
  **stub** — `FakeEmbyServer.rate_limit`, whose own docstring says it is the
  one behaviour in that file with no observation behind it — and provoking a
  real one is **refused with a reason**: the only servers this project talks to
  are a household's own media server and the live TMDb API, and hammering
  either until it rate-limits is precisely what
  [ADR-0040](decisions/0040-the-outbound-limiter-is-per-source-and-spaces-requests.md)'s
  outbound gate exists to prevent, and what
  [ADR-0005](decisions/0005-bulk-bootstrap.md) declined when it sized the crawl
  below TMDb's *stated* "somewhere in the 40 requests per second range" rather
  than discovering the real ceiling by hitting it. Evidence for this half would
  be evidence that the gate failed. The general form —
  **a refusal path that has never fired is pinned by its construction and not
  by an observation, and the honest closing note names which of the two the
  reader is getting** — is in
  `.claude/rules/ports-and-error-taxonomy.md`.
- 🔴 **`GET /images/{image_id}` catches two of the four families
  `port_error_for` returns, so a CDN 429 or 401/403 leaves the RFC 9457 envelope
  as a bare `500 text/plain`** — found by M10's F3 on 2026-08-20 while measuring
  something else, and confirmed independently in review. `port_error_for` answers
  429 with `PortRateLimited` and 401/403 with `PortAuthFailed`; **neither
  subclasses `PortUnavailable`**, and `get_image`'s ladder is `PortUnavailable` →
  `MediaTypeNotServable` → `PortDataMalformed`, so neither family is caught
  anywhere and Starlette answers before any handler can. **The evidence, with its
  control:** driven through a real `create_app()` with one dependency overridden,
  `PortUnavailable` answers `503 application/problem+json` (the control fires) and
  `PortRateLimited` and `PortAuthFailed` both answer `500 text/plain`; a reviewer
  reproduced it by driving the real `ProviderCdnImageFetcher` over an
  `httpx.MockTransport`, so the whole chain ran rather than a fake raising.
  **Never observed live**: 0 firings in F3's 250-request run against the real
  CDN, and the entry above records 130,750 requests to two upstreams that have
  never produced a 429 at all — so this is a defect nobody has met, which is why
  it survived M9's whole review. **Not a vocabulary question**, and that is what
  makes it small: `PortRateLimited` is unambiguously transient and
  `503 source_unavailable` with a `Retry-After` already exists one arm away, so
  the 429 half is an `except` tuple. Only the 401/403 half needs a decision — the
  CDN needs no credential, so a 403 means something *in front of* it refused,
  which is the captive-portal population wearing a status
  ([ADR-0030](decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)'s
  image amendment has the taxonomy). **F3 deliberately did not fix it**: its bar
  pre-registered the deliverable as "exactly this and nothing more" before the
  first request, and a behaviour change inside it would have been editing the bar
  after seeing the run. The gap was *pre-registered*, not discovered — the bar's
  classification table gave `escapes_the_route` its own bucket up front.
  **The transferable half is not about images**: a route's `except` ladder
  encodes an assumption about the shared `port_error_for` ladder that **nothing
  type-checks**, so any route catching a subset of what its adapter can raise
  leaves the envelope silently. Worth a scan across every router before this is
  called a one-route bug. 🔴 **And the project already knows this shape** — the
  identical escape is written into `adapters/bulk/download.py` and
  `adapters/bulk/movielens.py` as a scar (*"naming only one of them is what let a
  `PortRateLimited` escape uncaught from a caller that had guarded only against
  `PortUnavailable`"*), and `services/` carries a three-line test idiom for it
  **twice**: a fresh anonymous `UsherPortError` subclass asserted not to escape
  (`test_services_jobs.py::test_every_port_error_backs_off_rather_than_escaping`,
  `test_services_reconcile.py::test_reconcile_never_raises_a_port_error`). **No
  route in `api/` has one.** The guard stopped at the service boundary because
  that is where an escape kills a loop, while at a route it is one ugly response
  nobody is watching. That makes this cheap to fix properly rather than
  per-route: the recipe already exists in the tree.
  ⚠️ **Filed with it, because it is the same reader and the same file:
  `GET /images/{image_id}`'s three failure arms have no counter**, so nothing
  that ships can say how often any of them fires. That is what makes ADR-0030's
  reopening trigger for the image amendment un-checkable in its rate half, and
  it is the read-surface rule (`.claude/rules/ports-and-error-taxonomy.md`'s *"a
  filter is invisible without a counter"*) applied one layer up. The obvious
  repair — one `outcome`-labelled counter on `usher.images`, on
  `usher.images.references`' precedent — carries one real design question, which
  is that `configure_metrics` installs `metric_readers=[]` unless
  `telemetry_enabled`, so on a default deployment it would export nowhere and
  the trigger would be no more checkable than it is now.
- 🔴 **`test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`
  is intermittent under whole-suite load, and this list is where that belongs** —
  **1 failure in 5 whole-`tests/integration` runs, 0 in 5 runs on its own**,
  measured by M9's H7 on 2026-08-12 over the merged tree — and **it reproduced
  again the same day during H6's rework, once in two runs, taking the observed
  rate to 2 in 7.** Two things travel with that second reproduction and both
  matter more than the rate. **The failing run had `ruff`, `mypy` and
  `lint-imports` executing concurrently in another shell and the clean re-run
  did not** — which is one more datum consistent with load and is *still* not a
  mechanism, exactly as the two earlier attempts to explain a flake in this
  project by load were not. And **the failing run captured only `tail -2`, so
  which assertion lost was not recorded and is still unknown**; the re-run that
  would have shown it passed. *A flake reproduced without its traceback
  captured is a rate, not a mechanism* — whoever takes this should run it under
  load with the full output redirected to a file, because the reproduction is
  cheap and the diagnosis is the whole value. It is A6's
  serve-stale feature asserted at the HTTP boundary, and **two of its three
  claims are *ordering* claims** — the response arrived with the refresh still
  queued (`row_refreshes.depth == 1`), and the refresh's session began strictly
  after the request's ended. (H7's own write-up says all three; the first claim
  is that the served screen is the stale one, which no clock decides. Corrected
  here because *which* assertion is fragile is the whole content of this
  entry.) A loaded box is precisely where the other two are fragile. That is a
  property of the assertions, and **no run so far is evidence of a defect in
  serve-stale** — nobody has isolated which of the two loses, and until somebody
  does, "load" is a correlation and not a mechanism. That is the distinction
  this milestone had to relearn three times: once when a contention theory was
  relayed as settled and refuted by an isolated-copy bisect, once when a
  non-`NULL` `xmin` was asserted to mean an uncommitted read and measured not
  to, and once here.
  ⚠️ **It is deselected by node id for a mutation sweep and by nothing else.**
  `.github/workflows/ci.yml:46` runs `uv run pytest --cov=usher
  --cov-report=term-missing` over the whole suite with no deselection, so the
  first CI red it produces arrives with nothing anywhere saying it is known —
  which is the exact failure this section exists to prevent, and the reason it
  is recorded here rather than only in the sweep ledger and the plan.
  **The honest fix is to make the two ordering premises facts rather than
  races** — the case already stops the lane across the request, which is what
  makes claim 2 solid, so the unheld one is claim 3's *"the refresh's session
  began after the request's ended"*, observed through a session log rather than
  forced. Not to deselect it in CI: a case deselected in CI is a feature nobody
  checks.
- ⚠️ **A second intermittent integration group is *reported* and did not
  reproduce, and this list is where the report belongs rather than a rate.**
  Raised 2026-08-19 reviewing M10's S4:
  `tests/integration/test_adapters_search_postgres.py` run alone gave **1 / 0 /
  3 failures over three consecutive runs** on a copy verified byte-identical to
  `139a37c`, always the same three RRF-fusion cases
  (`test_a_single_lane_row_does_not_outrank_the_row_both_lanes_found`,
  `test_a_row_only_one_lane_found_is_still_returned`,
  `test_a_title_deep_in_both_lanes_still_reaches_the_first_page`).
  **Re-measured the same day: ten consecutive solo runs, `38 passed / 1
  skipped` every time, zero failures** — and neither obvious explanation
  survives, because S4 changed no file under `src/` (so the search path is
  byte-identical in both measurements) and the ten runs were taken at a load
  average of **9.59 on 16 cores** rather than on an idle box. Four failures in
  three runs and zero in ten do not average into a rate: they are two
  environments or one very low rate, and nothing here can tell them apart. It
  is recorded because **Phase 1's S11 runs a phase-wide mutation sweep scored
  on "did the run fail"**, which an intermittent case makes unsound in exactly
  the way the entry above describes — a plant whose only kill is one of these
  three would be a false kill. `.claude/rules/mutation-sweeps.md` carries the
  full measurement and the reason the affected sweep control's verdict stands
  either way. Chasing the mechanism is nobody's task yet, deliberately.
- ✅ **`test_sse_end_to_end.py::test_opening_a_stub_promotes_it…` was flaky and
  is closed — do not inherit the deselection.** `.claude/rules/mutation-sweeps.md`
  names it **four** times: one attribution note and **two deselections**, then
  H7's entry retiring both. (H7's write-up says *"nine ledger entries"*;
  `grep -c` says four. Counted rather than recalled, on the same rule as the
  commit and router counts in the plan's census.) Every one of those was stale
  by the end of M9: **G2 made the ordering structural**, publishing the frame after the
  completing commit, and `test_sse_end_to_end.py:434-441` documents G1's
  bounded `_job_xmin_settles` poll being *retired* rather than tidied —
  restoring it would hide the regression the single read now catches. H7
  measured the retirement: **5 of 5 whole-`tests/integration` runs, and absent
  from all fifteen whole-suite sweep runs' failure lists.** **A deselection
  inherited from a ledger is a deselection nobody measured**, and this one
  would have carried a green case out of the suite while the genuinely
  intermittent one above stayed in. The reading it produced is kept because it
  is a fact about the code rather than about the case, and it is this:
  second reading evaluated and largely refuted, measured by
  M9 (G1) on 2026-08-11 and recorded in
  [ADR-0033](decisions/0033-an-event-is-a-statement-about-committed-state.md).
  **All five `events.publish` sites in `src/` publish after their own subject
  has committed**, each driven against a committing session with a second
  connection reading the subject at the instant of the publish, so *"a client is
  told an event landed before the transaction that produced it committed"* is
  false of the event's subject at every site. The literal claim survives and is
  smaller: the open transaction at the instant of an `enrich` frame is
  `JobWorker`'s, and it holds only **the two `BACKFILL` enqueues
  (`enrich.py:270–277`) and the `DELETE` that completes the job** — measured, as
  `[('enrich','running')]` at the publish becoming
  `[('derive','pending'),('index','pending')]` after `complete()` + `_commit()`.
  A rollback there costs those two enqueues plus **one duplicate
  `title.updated`** on the `requeue_running` re-run; the title itself committed
  at `enrich.py:208` and is never at risk. The test's own failure is that
  residual window observed — `assert '745' is None` is **not** an uncommitted
  row being read (Postgres shows no such thing; `xmin` names the writer of the
  version the reader *can* see, and at failure it is the claim's committed
  `status='running'`), and it reproduces **5 of 5** with a delay planted between
  the handler returning and `complete()`, against 6 of 13 unplanted under load.
  **Closed by M9's G2** — the ordering is structural now, as an ordering
  property rather than a durability one, and it needed no outbox table.
- **Query expansion is shipped off after measuring worse** — M8's live
  verification measured MRR 0.733 → 0.373 and recall@10 0.800 → 0.533, with a
  label-free control (query-to-query cosine 0.5417 → 0.5975 mean, 0.6328 →
  0.7784 max) confirming the rewrites collapse toward the corpus centroid.
  ⚠️ One model, one 150-document corpus, five queries. The code ships behind
  `USHER_QUERY_EXPANSION_ENABLED` (default `false`) and PRD
  [05](05-search-and-similarity.md) carries the measurement against the claim.
  **Post-v1 unless M9's `search_queries` supplies a real evaluation set** —
  which is the thing that would actually settle it, and is the reason not to
  re-litigate it on five queries.
- ✅ **Expansion is billed on searches the semantic lane cannot serve —
  issue #16, closed.** The guard was `embedder is None`, not "anything is
  embedded". Measured: with `USHER_EMBEDDING_ENABLED=true` and
  `title_embeddings` empty, `usher search` bought a completion, printed the
  rewrite, returned `semantic_coverage=0.000`, and *then* said no title had an
  embedding — **the warning arrives after the money**, on every fused search of
  a not-yet-backfilled deployment. It is now
  `SearchIndex.semantic_coverage(filters) > 0.0`, asked in front of the
  expansion.

  ⚠️ **This entry's own pricing was wrong twice, and that is the transferable
  part.** *"The honest predicate is unanswerable before the vector that does
  the filtering exists"* — it is answerable: nothing in a `SearchFilters` is
  derived from a query vector, and `PostgresSearchIndex._COVERAGE` already
  took predicates and no vector. So the strong predicate was already computed,
  a few lines away, and only the *callability* was missing; the entry costed
  the weak stand-in it thought it was reduced to. And *"a read on every fused
  search"* is a consequence of where the read is put, not of having one: behind
  `expander is not None` it is bought only by deployments that expand, which is
  none by default. **A carried-debt entry that prices a fix is a claim, and it
  ages exactly like any other claim in this repository.**
- ✅ **The candidate pool has no ownership *filter*, only an `ORDER BY` key** —
  while the curation prompt asserted *"one household's **own** library"*.
  Reachable on any library with fewer than `USHER_CURATION_POOL_SIZE` unwatched
  owned titles. Interacts with `min_cards = 5`: M8 measured rows carrying 5–6
  cards at pool 200 and 2–3 at pool 5–8, so a household with a small unwatched
  pool gets **zero rows every time, at full price** — and filtering on ownership
  makes small pools more common. A product decision (filter, or correct the
  prompt's claim), not a defect to patch. **M9**, with the other row work.
  **Paid by M9 Task G3 on 2026-08-11: the prompt was corrected and the pool was
  not filtered**, on a pool-composition sweep through the real Postgres
  repository that made *"filtering makes small pools more common"* a number —
  the owned fraction of a 200-title pool runs 0.0% / 1.5% / 2.5% / 4.0% /
  10.0% / 100.0% for a household owning 0 / 3 / 5 / 8 / 20 / 200 unwatched
  titles, a filtered pool at 3 owned titles cannot fill one row, and **the
  filter could add nothing**, because `owned DESC` is the first sort key so the
  unfiltered pool already carries every unwatched-owned title there is. A
  per-candidate ownership marker was priced against the endpoint at 2.9–4.9
  prompt tokens a candidate, missed a bar of 2.0 declared before the
  measurement, and is not rendered; the corrected sentence costs +26 tokens
  once. Evidence and the arm not taken are in
  [ADR-0028](decisions/0028-the-pool-is-the-contract.md)'s dated amendment.
  ✅ **The second half of this entry — that `min_cards = 5` bills a small pool
  for nothing — was paid by M9 Task G4 on 2026-08-11**, and it is the half that
  was arithmetic rather than a decision. `CurationService.generate`'s
  empty-pool guard was widened from `len(pool) == 0` to
  `len(pool) < min_cards`, so a pool that cannot fill one row is refused in
  front of `complete_json`: no completion is bought, no `llm_calls` row is
  written, and the job parks on `PortDataMalformed` with a sentence naming the
  count and the floor. No new setting — `min_cards` crosses the prompt, the
  schema and the validator from one definition. ⚠️ **How often it can fire is
  the part to quote with it:** because G3 left the pool unfiltered, only a
  catalog whose *whole* unwatched set is below the floor reaches the guard, so
  it is rare rather than nightly and what it buys is the completion it declines
  on the run where it fires. Under the filtered arm it would have been the
  common case — and a park would then have been a permanent block on a
  transient condition.
- ✅ **`ports/repository.py` was 3,434 lines holding 19 ABCs, and it was the one
  layer that did not mirror `db/repositories/`** — found by M8's review
  2026-08-10, **paid by M9 Task A1 on 2026-08-11**, which is the entry this list
  exists to produce. Measured rather than estimated: 19 `(ABC)` classes, 107
  `@abstractmethod`s and 19 supporting dataclasses in one module. **99 files
  imported it**, so every service that wanted one port imported a module holding
  eighteen others, and it was where new ports went because it was where ports
  were: M8 added **616 insertions** to it (`git diff --numstat
  milestone/m7-rows..HEAD`) for about 30 lines of signature.
  **Two of this entry's own numbers were wrong and the split is what measured
  them.** *"19 sibling modules in `src/usher/db/repositories/`, one per
  aggregate"* implied a 19-to-19 mapping and **there is none**: of those 19
  modules, `credentials.py` implements `CredentialStore` and `jobs.py`
  implements `JobQueue` — declared in `ports/credentials.py` and `ports/jobs.py`,
  neither ever in this file — `_errors.py` is a helper, and three modules hold
  two repository ABCs each (`people.py`, `search.py`, `sync.py`). The real shape
  is **19 ports across 16 modules**, which is what shipped, plus one private
  `_results.py` for `BulkWriteResult`, the single type six ports across six
  modules return. And *"474 lines survive an `ast.unparse` of the
  docstring-stripped tree, so roughly 86% of the file is prose"* is **619 of
  3,434**, i.e. 82% — still the point it was making, and still the reason the
  move's only real proof was comparing `inspect.getsource` of all 38 public
  objects against `git show HEAD:` byte for byte.
  **The ports themselves were correctly sized and this was not a redesign.**
  Every class, docstring and signature crossed verbatim, `__init__.py`
  re-exports the lot, and **not one of the 99 importers changed** — the
  `import-linter` contracts were stated at `usher.ports` and still reported
  9 kept, 0 broken at A1 (the analysed-file count rises 160 → 177).
  ✅ **There is a tenth now, added by M9's H6, and the reason it is worth an
  entry here is that A1's own sweep measured the hole.** All nine of those
  contracts are stated at a **top-level package**, so an import *between two
  submodules of `usher.ports.repository`* is invisible to every one of them:
  inverting `_results.py` into `bulk.py` and importing it back the other way
  passes ruff, `ruff format --check`, `mypy` and all nine, and was caught only
  by one bespoke AST scan in one test file. The tenth is an `independence`
  contract over the nineteen aggregate modules — `testing-discipline.md`'s own
  *prefer a graph property wherever one is expressible* — and **its module list
  is the whole contract**, so
  `test_the_independence_contract_names_every_aggregate_port_module` derives the
  membership from the package rather than trusting the list. Measured: with
  `title` dropped from the list, `lint-imports` still reports **10 kept, 0
  broken** and only that case notices.
  **What made it finally happen is the part worth keeping**, because "owned by
  whoever adds the next port" had been true and inert for a milestone: pure
  churn with no behavioural claim never competes with work that has one. Four M9
  groups each add a port, so the split stopped being churn and became the thing
  that decides whether they collide. The invariant is therefore a **test**, not
  a convention —
  `tests/unit/test_ports_repository_package.py::test_every_postgres_repository_module_has_a_port_module_of_the_same_name`
  fails if a port lands anywhere but the module named for its aggregate, so a
  new port is a new file plus one import and one `__all__` entry, and nobody has
  to decide anything. ⚠️ One trap the split left behind, recorded because it is
  cheap to re-introduce: a `pkgutil.iter_modules` scan of `usher.ports` does not
  descend into a subpackage, so it silently stopped seeing all 19 repository
  ports (13 found against 32) while every control on it stayed green.
  `test_ports.py::test_every_port_abc_is_registered_in_all_ports` uses
  `walk_packages` for that reason.
- **A worker that dies on an unhandled `MissingGreenlet` orphans its claims in
  `running` for good, and at more than one worker there is no recovery** —
  found by M9's S3, in a 1.98-hour live TMDb enrichment of 130,647 titles, and
  it is the one finding of that run that is a defect in **shipped** code rather
  than a number. One of three workers crashed 78 minutes in; its **20 claimed
  jobs stayed `running` and nothing requeued them**. The mechanism is a pair of
  defaults that are each right alone: only `JobWorker.startup()` requeues a
  `running` row, and it calls `requeue_running()` with
  `older_than_seconds=0.0`, which is correct for the single-worker deployment
  M4 shipped and **steals the other workers' live claims** the moment there are
  two. So restarting the dead worker to recover its twenty jobs cancels the
  survivors' work, and not restarting it leaves the twenty parked forever —
  a dead end at N > 1. **No M9 task owned it** and it is not fixable inside a
  milestone's slack: the honest fix is a claim lease (a `claimed_by` or a
  heartbeat column, so `requeue_running` can name *whose* claims are stale),
  which is a migration, a port change and a change to every composition root.
  The cheap mitigation, which is documentation rather than code: run one worker,
  or accept that a crash costs a manual `UPDATE`.

  ✅ **Discharged by M9's W1 (2026-08-12), and the estimate was wrong in one
  place worth naming: no migration was needed.** The lease this entry asks for
  is `requeue_running(older_than_seconds=…)`, which the port has carried since
  M4, measured against `jobs.updated_at`, which the schema has carried since
  M4 — so the missing half was never a column, it was a **heartbeat to move
  it**. `JobQueue.touch()` is that, `JobWorker.recover()` passes
  `USHER_JOB_LEASE_SECONDS` instead of the `0.0` default, and it is called on
  a timer rather than once, which is what lets a live worker recover a *dead
  peer's* claims — the thing this entry says there is no way to do. A
  `claimed_by` column would have named whose claims are stale; a beat makes
  that question unnecessary, because a claim nobody is renewing is stale
  whoever holds it. The port change and the composition-root change were both
  real. [ADR-0037](decisions/0037-the-worker-is-a-bounded-pool-of-scopes.md).

  ⚠️ **The `MissingGreenlet` itself is not fixed and is not claimed to be.**
  This entry's opening clause — *"a worker that dies on an unhandled
  `MissingGreenlet`"* — describes a crash whose cause is still unknown; what W1
  removed is the **consequence**, which is that its claims were unrecoverable.
  The hypothesis that it was a shared `AsyncSession` is refuted by the
  deployment shape: `usher work` held one session and ran one job at a time, so
  there was no second coroutine to touch it. `.claude/rules/tmdb-and-enrichment.md`
  carries the refutation and what a next run has to capture.
  🔴 **And the worse half, which the `usher work` description above understates:
  the same fault inside the API server's own in-process worker lane orphans
  claims with no process death at all.** ✅ *Discharged with the entry above:
  the lane now calls `recover()` on a timer, so a pass that raised does not
  strand its claims until the process restarts.* `api/lanes.py` called
  `worker.startup()` **once per lane lifetime** and set `requeued = True` —
  correct on its own terms, and the comment says why (*"a second call would
  steal this lane's own claims"*) — while `:573-578` catches `except Exception`,
  logs a warning and continues, which is also correct on its own terms
  (*"a database outage must slow the lane down, never end it"*). Composed, they
  are a leak: a `MissingGreenlet` raised inside `run_once()` leaves that pass's
  claims in `running`, the lane loops round and claims fresh work, **no
  `startup()` ever runs again in that process**, and nothing appears in
  `/health/ready` — the very thing the `except Exception` comment says a
  returning lane would cause is what the surviving lane silently produces for
  the abandoned claims. In the `usher work` case above an operator at least
  sees a dead worker. Here the only symptom is a queue that never finishes some
  rows, and a `logger.warning` in a stream nothing asserts on. **The claim lease
  is the fix for both**; until then this path is the one to name first, because
  it is the deployment shape `docker compose up` gives you by default.
- ✅ **`usher unmatched --resolve` stack-traced on an unknown `--title`** —
  found by M9's E4, which fixed the *route* and could not fix the CLI because
  `cli.py` is not that task's file. The route read the title first and answered
  a problem document; the command handed the id straight to `attach_title`, and
  against Postgres that is a foreign-key violation translated to
  `RepositoryConflict` — which is **not** in `cli.OPERATOR_ERRORS` (verified:
  no member of that tuple is a base of it), so an operator who mistyped a UUID
  got a stack instead of a sentence.
  ✅ **Paid on 2026-08-18 (issue #5), and the taxonomy question this entry was
  waiting on was the wrong one to have been waiting on.** The entry read *"it
  is a one-line change and it is carried rather than taken because the family
  is an argued taxonomy, not a list — the question ADR-0026 asks before adding
  a member is how often an operator hits it"*. The fix is **not** a tenth
  member of `OPERATOR_ERRORS`: it is the route's own `SELECT` in front of the
  write, so the command answers `no such title: <id>` and `RepositoryConflict`
  keeps every stack it had. That leaves
  [ADR-0026](decisions/0026-the-cli-boundary-names-families.md)'s line exactly
  where it was, which is why this cost no argument about the family — the entry
  sized the work as "decide whether a refusal type is operator-facing" when the
  available fix was "do not raise one".
  **Reproduced before it was fixed**, against `pgvector/pgvector:pg17` at
  `alembic head` with one seeded source and one unmatched item: the traceback
  ended in `RepositoryConflict: cannot attach media item <media item id>` —
  **naming the id that was correct**, which the entry above did not record and
  which is half of why the shipped output diagnosed nothing.
  **The other arm of that branch was checked and needed nothing.** An unknown
  `--resolve` is not symmetric with an unknown `--title`: the `UPDATE` matches
  no row, so the foreign key is never evaluated, and `attach_title`'s boolean
  has answered `no such media item` since M4. Confirmed on the same database.
  Both refusals therefore print and return rather than raising `SystemExit` —
  one command naming two things that do not exist owes them one exit code.
  📎 **M10's F4 answered the frequency question anyway, on 2026-08-20, and the
  answer is kept because it is what ADR-0026's Consequences now carries.** It
  refuses the one-line change on its own terms: **22 raise sites across 14
  modules**, of which **exactly one is reachable from a CLI argument** — this
  one. `usher bootstrap --phase` can reach `ImportRunRepository.start`'s
  uniqueness conflict, but by racing another bootstrap rather than by a typo,
  and `BootstrapService._concede_to_other_owner` answers it without raising;
  every other site is reached only by a walk (`similar --rebuild`, `derive`,
  `curate`, `search`/`suggest`, `work`, `sync`, and `push`/`serve`, which run
  the lanes). `db/repositories/source.py`'s two split across those groups:
  `add` only from `POST /admin/sources`, since no subcommand adds a source, but
  **`update` is not reachable from that route at all** — its one caller is
  `api/lanes.py`'s `_write_push_available`, so it is CLI-reachable and not
  argument-reachable. So widening the tuple would mute 22 sites to fix one,
  several of them the deliberate bug tripwires that ADR's amendment names.
  *(F4 also measured the frame count this entry used to quote as "sixty": at a
  real terminal it is **40**, and the 60 came from a pytest run's 62 with 25
  `_pytest`/`pluggy`/`pytest_asyncio` harness frames in it.)*
- **A covering index for `GET /admin/unmatched` is measured, requested, and
  declined** — M9's E4, over 200,000 items / 70,000 unmatched / 23,333 undated.
  `ix_media_items_unmatched` is `(source_id) WHERE title_id IS NULL` and carries
  **neither sort key**, so every page top-N sorts the whole unmatched
  population: keyset page 1 is **16.4 ms / 966 buffers / a top-N heapsort**, the
  deepest keyset page **1.9 ms**, and the offset spelling the route does not use
  runs 17.4 ms at page 1 and **57.3 ms at `OFFSET 69,900`, spilling to disk**.
  So **the keyset fixed the depth and not the page** — which is worth knowing
  before anyone reads ADR-0034 as having fixed both. A covering
  `(added_at DESC NULLS LAST, id DESC) WHERE title_id IS NULL` removes the sort.
  Not authorised in M9: 16.4 ms on an admin review queue is acceptable and a
  fourth migration for it is not worth the milestone.
- ~~**`/openapi.json` describes every problem response at `application/json`
  while the wire sends `application/problem+json`**~~ — measured by M9's H2 and
  reported rather than closed. FastAPI renders
  `responses={404: {"model": ProblemResponse}}` under the *route's* response
  media type, so the document was wrong about the one header RFC 9457 makes
  load-bearing. Spelling the media type in forks `test_api_playback.py`'s and
  `test_api_watch.py`'s assertions, which read `content["application/json"]`,
  and H2 judged that it "buys a client nothing it cannot read off the `type`
  member" — so its conformance check asserted the response **shape** and not
  the media type, and said so in its own docstring.
  ✅ **Both sites said so where the fix would land** — the milestone's final
  review found the two assertions reading `content["application/json"]` with no
  comment naming why, so the *cost* of the fix was documented everywhere except
  at the two places that have to change. Corrected 2026-08-12: each carried the
  known-wrong marker and pointed at `tests/unit/test_api_openapi.py`. **A debt
  recorded only in the roadmap is a debt the person editing the code does not
  see** — the same shape as the curation role sentence corrected in
  `testing-discipline.md` this same day, one subsystem over. **It worked**:
  the fix below found both assertions from the markers.
  ⚠️ **And the second marker misnamed its own twin**, which is worth keeping
  because it is the failure mode the correction was written to prevent. Both
  markers said the fix *"would fork this assertion and its twin in
  `test_api_watch.py`"* — correct in the playback file and a **self-reference**
  in the watch file, whose twin is in the playback file. A marker that points at
  itself is a marker that survives a grep for the thing it was meant to make
  findable. Both are deleted now that the fix has landed.
  ✅ **Taken 2026-08-19 (issue #6), and the reason it was carried is the part
  that did not hold.** *"Buys a client nothing it cannot read off the `type`
  member"* is a claim about a client that has already decided to parse the body
  as a problem document; a generated one decides that from the **declared media
  type**, before it parses anything — and the console that ships in this
  repository generates against this document with `openapi-typescript`
  (`web/package.json`'s `gen:types`). Measured before: **56** responses across
  **35** operations and **92** response bodies carried a `ProblemResponse`,
  every one of them declared `application/json` and none
  `application/problem+json`, against a wire that answered
  `application/problem+json` on all five vocabulary members reachable without a
  database. The fix is `UsherAPI.openapi` in `api/app.py` — a post-pass over the
  generated document, keyed off the `$ref` rather than off a status list,
  because FastAPI offers no per-response media type and dropping `model=` to
  hand-write `content` would leave `ProblemResponse` out of `components` and
  every ref dangling. **Keying on the schema is also what excludes
  `GET /health/ready`'s 503 by construction** — its model is
  `ReadinessResponse` — rather than by a second exemption list beside
  ADR-0030's `PROBLEM_EXEMPTIONS`; `model=` stays at all 20 declaration sites
  across 14 router modules and no route decorator changed.
  `test_api_openapi.py` enumerates rather than samples, over a floor and an
  exact non-problem count so that a walk which matched nothing and a rewrite
  that moved a **200** both fail; a second case compares the declared type to
  the one three real routes actually send. The cost was real but smaller than
  stated: five assertions in two files, of which three move and two stay — a
  **200** really is `application/json`, and `test_api_watch.py`'s case reads
  better for saying so, since the 404 and the 200 on one operation are now two
  different media types.

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
- ~~**Reference client** — separate repository.~~ **Done, and not as written.**
  Shipped 2026-08-19 as **Usher Console** in `web/`, in *this* repository and in
  the same container, served at `/console` by `usher.api.console`. The candidate
  said "separate repository" and that was tried: `usher-web` existed for a day
  and its whole nginx layer was a `/api/*` → `/*` rewrite, which is what made
  `POST /play`'s ticket URL — minted from the incoming `Host` header — point at
  the wrong port for an external player. **A client in another repository cannot
  be versioned with the API it generates from, and the proxy that a second
  origin needs is where the defect lived.** The design system it implements is a
  handoff bundle (28 components, 18 screens, a 103-pair contrast ledger); the
  behavioural authority is `web/docs/patterns.md`.
- **Request/wanted list** — titles in the catalog but on no source.

## Explicitly out of scope

Transcoding, file management, downloading or acquisition, multi-tenant hosting,
commercial use, and collaborative filtering.
