---
paths:
  - "docs/plans/**"
  - "docs/prd/09-roadmap.md"
---

# What each milestone delivered, verified, and did not build

A decisions register, loaded when planning or reading a milestone. **The value
here is the refusals** — each was stated with its reason in that milestone's plan
and in [PRD 09](../../docs/prd/09-roadmap.md), and is repeated here so a later
reader does not re-open a settled call. Live-verification *evidence* goes in the
subsystem rules file, not here.

## Delivered, and live-verified against

| | delivers | live-verified against |
|---|---|---|
| **M1** | scaffold, config, domain models, port ABCs, persistence, telemetry, health routes, container + compose + CI | — |
| **M2** | bulk bootstrap — IMDb skeleton, TMDb id export, Wikidata crosswalk, all resumable | the real dumps and live WDQS |
| **M3** | the Emby `SourceAdapter`, encrypted credentials, admin source routes, a source-agnostic contract suite | Emby 4.9.5.0 |
| **M4** | the ingest pipeline — match/ingest/reconcile/watch-sync/enrich over nine ports and a Postgres priority queue, the TMDb provider, the CLI | Emby 4.9.5.0, live TMDb v3 |
| **M5** | the push lane, supervised reconnect with a gap-closing delta, `GET /titles/{id}`, `GET /events` over SSE | Emby's `/embywebsocket` |
| **M6** | `search_document` + GIN, trigram type-ahead, embeddings, `title_neighbors`, RRF fusion, the search CLI | a real 1.27M-title catalog |
| **M7** | the composed home screen — nine row providers, `HomeService`, `TasteService`, `DeriveService`, the tag genome, `GET /home` | a real 1.27M-title catalog |
| **M8** | LLM curation end to end — `OpenAICompatibleClient`, `curated_rows` + `llm_calls`, the candidate pool, `CurationService` and its validator, `CuratedProvider`, `JobKind.CURATE`, `POST /admin/rows/regenerate`, `usher curate`, the genome tag vocabulary, query expansion | a local vLLM over a real 1.27M-title catalog |
| **M9** | the whole HTTP surface — PRD 07's Screens, Resources, Actions, Admin and Meta behind one RFC 9457 envelope over a closed seven-member `code` vocabulary; keyset cursors; search, two-tier suggest, browse, similarity, the series hierarchy; the image proxy and `RowCard` artwork; `/play` with the playback ticket and watch write-back; the admin completion; `search_queries`; `GET /meta/attribution`. **Track 2:** `append_to_response=season/N`, the IMDb akas and credit-names expansion, the priority-tier TMDb crawl | live TMDb v3, the real IMDb dumps, Emby 4.9.5.0 (the Emby half ran after the gate) |

## M9's eight boundary calls

1. **No authentication**; `current_user` still returns the singleton default
   user. Designing authorization against routes landing in the same milestone is
   the mistake PRD 07 avoided four times with the error envelope.
2. **The GIN → GiST swap for tier-2 suggest is deferred, not rejected**
   (ADR-0031). The 2.8-point recall gain was measured against synthetic mutations
   of real titles; `search_queries` is the evidence that would settle it and has
   no rows until M9 ships. The two indexes also **cannot coexist** — a GiST
   trigram index beside the GIN one makes the planner take GiST for `%` and costs
   the shipped path 4.3× on p50 for identical recall. A *replacement* decision.
3. **No Meilisearch**, unchanged from M6's call 7.
4. **No byte proxying for playback** — the ticket is a `302`. **Images *are*
   proxied**: an image is small, cacheable and reusable across households; a
   video stream is none of the three.
5. **No per-client scoped tokens** — ADR-0012's option 2 needs a client identity
   that does not exist until authentication does (call 1).
6. **No scheduler.** The write-back retry rides the existing job queue with
   `run_after`. M8's call 8, unchanged.
7. **Query expansion stays off by default.** M8 measured it worse, and a
   milestone does not re-litigate a measurement by flipping a default.
8. **The 45 columns that leak a raw driver exception are still not translated** —
   31 of the 45 go through `copy_records_to_table`, whose `OverflowError` carries
   no SQLSTATE (measured; `db-and-sql.md` has it). Carried debt in PRD 09 with
   the candidate fix named.

**There is no ninth call.** M9's one shipped gap — live Emby verification of
playback and watch write-back — was closed 2026-08-12. It was a gap, not a call.

## M9 Track 2 — the IMDb bulk expansion

🔴 **Superseded by [ADR-0036](../../docs/prd/decisions/0036-the-imdb-tmdb-provenance-rule.md);
read it before acting here.** Two of the three original reasons do not survive:
the 2.0 GB ceiling was derived from a PRD 08 *resource envelope* row no code or
policy reads, and "people cannot be merged across the two sources" overstated a
qualified fact (`GET /person/{id}/external_ids` answers a person's `nconst`).
What survives is that `credits` could not dedupe an IMDb load — now closed by
`m09d`'s natural key. **T4's provenance rule is built; the `m09b` withdrawal
stands, because `m09c` took its position, not because the design failed.**

**What shipped is the names-only design, not a shrunken bulk people/credits
load** — that option failed on 2.702 GB against a 2.0 GB ceiling, which is why
there is no `m09b`.
`titles.credit_names` is filled from `title.principals` × `name.basics` with the
join resolved **in the importer**; **no person row and no credit row is written
from IMDb at all**, so the two bulk sources never own one entity. `title.akas`
lands in `m09a`'s `title_search_names`. Four of the IMDb people files' six
columns are dropped — they have nowhere to land, and a TMDb credit entry carries
no `nconst`, so the only shared merge key is a name, which by ADR-0003 is not
identity. Measurements are in `bootstrap-and-datasets.md`.

## After M9, on `main`

**Eight things landed after M9's gate closed** that the milestone plans cannot
see. This is the index, so none is re-litigated as "not built" from a plan that
predates it. Re-derive with
`gh pr list --repo anirudhlath/usher --state merged --json number,title,mergedAt`.

- **The bounded worker pool** — ADR-0037, PR #4. `JobWorker` takes a scope
  *factory* (one `UnitOfWork`, one event buffer, one source resolver per job) and
  `KIND_CONCURRENCY` resolves against `USHER_JOB_CONCURRENCY` (default 12);
  recovery is a lease with a heartbeat, not one `requeue_running()` at start.
  **It corrects two PRD sections a plan may still quote** — PRD 01's concurrency
  table and PRD 08's recovery rule. `api-telemetry-and-lanes.md` is the record.
- **`m09e`/`m09f`** — `halfvec(384)` → `halfvec(1024)`, deleting every embedding,
  centroid and neighbour row (ADR-0038), then every `halfvec` column to `PLAIN`.
- **The rating-provenance split** — `m10a` (ADR-0040). **Two of the three renames
  take a `tmdb_` prefix and the first does not, so "renamed to `tmdb_*`" is not
  derivable**: `community_rating` → **`tmdb_vote_average`**, `vote_count` →
  `tmdb_vote_count`, `popularity` → `tmdb_popularity`. No rating value was
  migrated; they were re-imported by `usher bootstrap --phase ratings`. The
  rollback table `titles_rating_backup_20260819` **is not dropped without the
  operator's say-so.**
- **The console** — `web/`, React 19 + Vite, served at `/console`.
  `mount_console` lives in `api/console.py`, not `api/app.py`. Its gate is not
  the Python gate: `console.md` and `web/CONVENTIONS.md` are the record.
- **E1, the quality-eval harness** — `src/usher/eval/`, the `eval` extra, `usher
  eval`, two more import contracts. One surface (`suggest`); E2–E4 are not
  planned. Three `fuzzy recall_at_5` bars are `pending` on #39. `evals.md`.
- **The resumable watch lane** — `m10b`, ADR-0042. Two behaviours came from
  review rather than the plan and are the ones a later reader will want to undo:
  `SyncRunRepository.save` is non-destructive (`completed` absorbs), and a
  resumed walk stamps its merges with the attempt's instant, not the original
  run's. ⚠️ **The code shipped and issue #41 is still OPEN** — the remaining step
  is the operator's, a watch walk run to completion. Check
  `gh issue view 41 --repo anirudhlath/usher` before reading the merge as done.
- **`VisibilityService`** — the plural half of PRD 03's demand lane. The module
  docstring is the record.
- **Enrichment-driven shelf staleness** — the row cache had two invalidation
  triggers and enrichment was not one, so an enriched title's shelves kept
  serving the pre-enrichment card until TTL. PRD 06 amended; `rows-and-genome.md`.

**Still not built** (re-verify against the tree, not this list): no auth module
and no `current_user`; no scheduler; `query_expansion_enabled` is `False`; no
GiST trigram index; `curation_pool_size` defaults to 200, capped at 1000; no
`usher.llm.*` metric (those names are span attributes); `copy_records_to_table`
is still on the raw driver.

## M8's eight boundary calls

**`LiteLLMClient` is NOT built** — the client is one `POST
/v1/chat/completions` over the httpx stack already here, because `base_url` *is*
the provider abstraction; litellm priced at **+146 MB and 29 distributions
against +0 and 0**, and three PRD sections naming it since M1 are corrected
rather than implemented (ADR-0027). **Generation is a job**; `POST
/admin/rows/regenerate` enqueues and returns 202, because a synchronous route
would be the first whose honest answer is *"the upstream is down"* and would
force PRD 07's envelope a milestone early. **The prompt addresses candidates by
integer index**, because an index is *bounds-checked* and a hallucinated UUID is
not. **The validator coerces before it compares** and counts five drop reasons.
**The candidate pool degrades without an embedder** and the taste centroid only
re-ranks it, because implementing PRD 06's *"pre-filtered by taste-centroid
proximity"* literally makes curation never fire on the shipped default.
**`llm_calls` carries NO `user_id`**, deliberately — it is a cost ledger joined
to outcomes through `curated_rows.generation_id`. **No `usher.llm.*` metric**,
because PRD 10 puts spend on Postgres; the two shipped metrics are about whether
the *validator* is eating the output. **Nothing schedules the nightly run.**

**M8's live run: every boundary call held**, and the finding that transfers is
that **the prompt's grouping instruction is not self-enforcing and nothing in
this system checks it** — 88% of generated headings were genre labels the prompt
forbids, so the curated shelf was substantively what `GenreAffinityProvider`
already produces from a `SELECT`. Evidence in `curation-and-llm.md`.

## M7's nine boundary calls

**`GET /home` IS built** (the first client-facing route since M5, because
ADR-0006's *"one request paints a screen"* is a property of a request boundary no
CLI can exhibit); **the `curated_rows`/`LLMRow`/`CuratedProvider` family is M8's
whole**, so `RowFamily` ships with two members rather than a `CURATED` nobody can
emit; **`RowCard` carries no artwork field**, absent rather than null — ✅
discharged by M9, which is the outcome the call named and not a reversal, so do
not re-litigate the absence; **`Person`/`Credit`/`Collection` ARE built**,
re-derived from `raw_payloads` with no second network call, minus `Person`'s four
`/person/{id}` fields; **weight class B is filled** and needs a denormalised
`titles.credit_names` because a generated column cannot reach another table;
**`title_search_names` is still not built** (M7 lands people, not aliases) — ✅
also discharged by M9 (`m09a`), same caveat; **the tag genome IS built** as one
dense `halfvec(1128)` per title rather than a tall table; **rows build
sequentially** because `AsyncSession` is not concurrency-safe; and **row provider
enable/disable does not become a table**, because its only writer would be an M9
route.

## M6's nine boundary calls

**No HTTP route** (the CLI delivers all four capabilities); **weight class B is
reserved and empty** (no `Person`/`Credit` table exists); **no
`title_search_names`** (with no aliases and no people it would duplicate four
columns of `titles`); **embeddings cover the enriched tier only** (a skeleton's
document is a generated column and needs no `index` job); **no new client event**
(`EnrichService` already publishes `title.updated`); **no query expansion**
(`ports/llm.py` has no implementation until M8); **no Meilisearch regardless of
the gate**; **similarity blends the two signals that have data** (embedding
cosine plus genre/keyword Jaccard); and **the `usher.db.staging` shared-table
lock is fixed here**, because M6's per-title `index` enqueue is what makes it
hurt.

**ADR-0002's typo-tolerance gate FAILED** on both halves of a bar written down
before the numbers were known: 27.8% for a 2–4-character name against 0.75,
68.3% for 5–7 against 0.85, transposition at 2–4 characters **0.0%**, and no
configuration within **6×** of a 50 ms budget. **Above 8 characters it needs
nothing** (91% of the catalog by row count). The deliverable was the recorded
failure, ADR-0002 amended, one shipped default changed, and the two-tier suggest
scoped to M9. Table in `search-and-embeddings.md`.

## M4's four boundary calls

The **index** stage is M6's (a job kind whose handler is a stub is a queue that
grows forever); **push/reconnect-delta/demand/SSE** are M5's (M4 builds the
promotion *mechanism* but nothing calls it with `JobPriority.DEMAND`); the
**three admin HTTP routes** are M9's, with the same capability delivered through
`usher.cli`; and enrichment populates `Title`/`Season`/`Episode` only, with
`Person`/`Credit`/`Collection`/`Image` re-derived from `raw_payloads` by M7/M9
with **no second network call**.
