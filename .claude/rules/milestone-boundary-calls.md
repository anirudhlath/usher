---
paths:
  - "docs/plans/**"
  - "docs/prd/09-roadmap.md"
---

# What each milestone delivered, verified, and did not build

Loaded when planning or reading a milestone. Every entry names a call that a
later reader would otherwise re-litigate — each was stated with its reason in
that milestone's plan and in [PRD 09](../../docs/prd/09-roadmap.md), and is
repeated here because the plans are long and this is the part that gets lost.


## What each milestone delivered, and what it was live-verified against

Moved out of `CLAUDE.md` on 2026-09-01: it is milestone history, it goes stale
on every merge, and it was being charged to every session in the repo to answer
a question only a planning session asks.

| | delivers | live-verified against |
|---|---|---|
| **M1** | scaffold, config, domain models, port ABCs, persistence, telemetry, health routes, container + compose + CI | — |
| **M2** | bulk bootstrap — IMDb skeleton, TMDb id export, Wikidata crosswalk, all resumable | the real dumps and live WDQS |
| **M3** | the Emby `SourceAdapter`, encrypted credentials, admin source routes, a source-agnostic contract suite | Emby 4.9.5.0 |
| **M4** | the ingest pipeline — match/ingest/reconcile/watch-sync/enrich over nine ports and a Postgres priority queue, the TMDb provider, the CLI | Emby 4.9.5.0 and the live TMDb v3 API |
| **M5** | the push lane, supervised reconnect with a gap-closing delta, `GET /titles/{id}`, `GET /events` over SSE | Emby's `/embywebsocket` |
| **M6** | `search_document` + GIN, trigram type-ahead, embeddings, `title_neighbors`, RRF fusion, the search CLI | a real 1,271,138-title catalog |
| **M7** | the composed home screen — nine row providers, `HomeService`, `TasteService`, `DeriveService`, the tag genome, `GET /home` | a real 1,271,570-title catalog |
| **M8** | LLM curation end to end — `OpenAICompatibleClient` (litellm declined), `curated_rows` + `llm_calls`, the candidate pool, `CurationService` and its validator, `CuratedProvider` as the tenth provider, `JobKind.CURATE`, `POST /admin/rows/regenerate`, `usher curate`, the genome tag vocabulary, query expansion | a local vLLM serving `gemma-4-26b-a4b` over a real 1,271,138-title catalog |
| **M9** | the whole HTTP surface — PRD 07's Screens, Resources, Actions, Admin and Meta tables behind one RFC 9457 envelope over a closed seven-member `code` vocabulary; keyset cursors; search, the two-tier suggest, browse, similarity, the series hierarchy; the image proxy (`images` + `GET /images/{id}`) and artwork on `RowCard`; `POST /titles\|episodes/{id}/play` with the playback ticket and outbound watch write-back; the admin completion (sync, unmatched, bootstrap status + trigger, row-provider toggles, `bootstrap.progress`); `search_queries` whole; `GET /meta/attribution`. **Track 2:** `append_to_response=season/N`, the IMDb akas and credit-names bulk expansion, the priority-tier TMDb crawl | the **live TMDb v3 API**, the real IMDb dumps, and a real **Emby 4.9.5.0** — ⚠️ the Emby half ran *after* the gate, for a reason spelled out below |

### M9's live verification, and the evidence rule it carries

Split out of the table above on 2026-09-02 — it had been a ~900-word cell,
which is unreadable in a table and is the only reason it is prose here.

- **TMDb.** S3 made **130,334 requests over 1.98 h** and enriched **130,647
  titles**. T2/T3 ran against the real IMDb dumps.
- **Emby.** H4/H5 ran 2026-08-12 in **23 bounded requests with no walk**:
  `/play` → ticket → `302` → a real **206** carrying `video/x-matroska` bytes,
  the play body leaking nothing with its positive control fired first, and the
  watch write-back read back *from Emby* and restored **byte-for-byte**.
- ⚠️ **They ran after the milestone closed**, because M9 had recorded "no Emby
  credentials on this host" having checked `~/code/usher/.env` and nowhere else
  — the operator's credentials were in a secrets file one directory over.
  **A negative established by looking in the one place the answer was expected
  is not a negative.** That is why this is quoted rather than summarised.

## M9's eight boundary calls

**Eight things M9 deliberately did not build**, each stated with its reason in
the M9 plan's *Boundary calls* section and in
[PRD 09](../../docs/prd/09-roadmap.md), and repeated here because that plan is
**9,269 lines** (`docs/plans/2026-08-10-m9-api-surface.md`, measured
2026-09-02; it read 8,868 here until then, which was the count at the plan
commit and not after the milestone's own follow-ups). Track 2's separate
refusals — the IMDb entity design that failed
its own size bar, and the five smaller calls that followed from it — are the
section below this one.

1. **Authentication is not built** and `current_user` still returns the
   singleton default user, because designing authorization against routes that
   land in the same milestone is the mistake PRD 07 avoided four times with the
   error envelope. M9 is where *that* deferral is finally cashed, by designing
   the envelope against routes that already exist — which is the argument for
   not repeating it one layer up.
2. **The GIN → GiST swap for tier-2 suggest is deferred, not rejected, and
   ADR-0031 says which.** The 2.8-point recall gain was measured against
   synthetically mutated real titles rather than anything a person typed;
   `search_queries` is the evidence that would settle it, M9 built that table,
   and it has no rows until after M9 ships. The two indexes also **cannot
   coexist** — a GiST trigram index beside the GIN one makes the planner take
   GiST for `%` and costs the shipped path 4.3× on p50 for identical recall — so
   this is a *replacement* decision, not an addition.
3. **No Meilisearch, unchanged from M6's call 7.**
4. **No byte proxying for playback.** The ticket is a `302` and the client
   fetches the target directly. **Images *are* proxied**, and the asymmetry is
   the subsystem rather than an inconsistency: an image is small, cacheable and
   reusable across households; a video stream is none of the three.
5. **No per-client scoped tokens** — ADR-0012's option 2 needs a client identity
   that does not exist until authentication does, which is call 1.
6. **No scheduler.** The write-back retry rides the existing Postgres job queue
   with `run_after`, so the one periodic thing this milestone added needed no
   new mechanism. Building a scheduler for it would be a second unplanned
   milestone inside this one — M8's call 8, unchanged.
7. **Query expansion stays off by default.** M8 measured it worse and a
   milestone does not re-litigate a measurement by flipping a default. What
   would settle it is a real evaluation set out of `search_queries`, which is
   carried debt rather than a boundary call.
8. **The 45 columns that leak a raw driver exception are still not translated.**
   M9 is the milestone that built the problem-code vocabulary such a leak would
   map onto, so it is where "widen the `except`" was cheapest to try — and the
   measurement that stops it (31 of the 45 go through `copy_records_to_table`,
   whose `OverflowError` carries no SQLSTATE) is in `db-and-sql.md` beside the
   staged-`COPY` CHECK entry (moved 2026-09-01).
   Left in PRD 09's carried debt with the candidate fix named.

✅ **There is no ninth call. M9's one shipped gap — the live Emby verification
of playback and watch write-back — was closed on 2026-08-12 and is written up
once, under "M9's live verification" above; it was a gap, not a call, and this
section holds only calls that were decided.** It is named here because the
sentence that stood in its place said the opposite. Per this file's convention
the evidence half is in `emby-push-and-ingest.md`, not here.

## M9 Track 2 — the IMDb bulk expansion, and the bar that failed

🔴 **Superseded 2026-08-12 by
[ADR-0036](../../docs/prd/decisions/0036-the-imdb-tmdb-provenance-rule.md), and
read that before acting on anything in this section.** Two of the three reasons
below do not survive scrutiny. **The 2.0 GB ceiling was not a constraint** — it
was derived from PRD 08's `~8–12 GB`, one row of a table headed *Resource
envelope*, which no code, host or policy reads; against a bar with a forcing
function the design measures 3.375 GB, 13.5% of a 25 GB ceiling. **And "people
cannot be merged across the two sources" was an overstatement** of a correctly
qualified fact: `GET /person/{id}/external_ids` answers a person's `nconst`, so
the merge costs one request each (887,161 of them, ~9.9 h) rather than being
impossible. What survives is the third reason — `credits` could not dedupe an
IMDb load — and that is now closed by `m09d`'s natural key rather than by a
refusal. **T4's provenance rule is built; the `m09b` withdrawal stands, but
because `m09c` took its position, not because the design failed.** The
measurements are in `bootstrap-and-datasets.md`; what follows is the record as
it stood.

**The headline call is a measured refusal and it is the one a later reader
will otherwise re-open: (A) failed on 2.702 GB against a 2.0 GB ceiling, so
there is no `people`/`credits` bulk load and no `m09b`.** The bar was written
to `/tmp/m9-t3/BAR.md` **before the first byte was downloaded** and had three
clauses; two passed comfortably (12,626,452 retained credits against a 20M
ceiling, 3,211,941 people against 6M) and the third failed on both readings of
its unit — **2,701,697,024 B, i.e. 2.702 GB or 2.516 GiB**. Stripped to the
five columns a credit cannot do without, with `people` unchanged, it is still
**2.395 GB, 20% over**. So *there is no version of (A) that fits by trimming*,
which is exactly why the fallback was written first.

**What shipped is the names-only design, and it is not a shrunken (A).**
`titles.credit_names` — a `text[]` M7 already added, which weight class B of
`search_document` already indexes — is filled directly from
`title.principals` × `name.basics`, with the join resolved **in the importer**
against a 345 MiB in-memory index because there is no `people` table for its
right-hand side to live in. **No person row and no credit row is written from
IMDb at all**, so the two bulk sources never own one entity and the provenance
rule that design needed does not exist. `title.akas` lands in `m09a`'s
`title_search_names`. Neither phase mints a table: **`alembic heads` stays at
exactly one (`m09c`)** *(as of that date — still exactly one on 2026-09-01, but
the head is `m10b`, with `m09d`, `m09e`, `m09f`, `m10a` and `m10b` landed
since)*, the `m09b` grant is withdrawn, and T4 (the provenance rule) and
T6-as-a-credits-writer are withdrawn with it.

**Five smaller calls, each stated with its reason.**

1. **The IMDb people files are read and four of their six columns are
   dropped.** `birthYear`, `deathYear`, `primaryProfession` and
   `knownForTitles` have nowhere to land once (A) fails, and filling
   `Person`'s four `/person/{id}` fields from them is impossible anyway: a
   TMDb credit entry carries no `nconst`, so the only merge key the two
   sources share for a person is a name, which by ADR-0003 is not identity.
2. *(Items 2–5 — `fill_credit_names`' skeleton-only precedence, the
   backfill-before-crawl ordering, `replace_aliases`' `imdb_ids` + `kind`
   scoping, and the contiguity measurement in place of a sort-order guard —
   moved 2026-09-01 to the tail of `bootstrap-and-datasets.md`, which already
   held their measurements.)*

## After M9, on `main` (verified 2026-09-02)

**Eight things landed after M9's gate closed** that the sections above cannot
see, each with its own record elsewhere. This is the index, so that none of them
is re-litigated as "not built" from a milestone plan that predates it. Listed by
merge date. It said "six" and omitted the first and last of these until
2026-09-02; re-derive it with
`gh pr list --repo anirudhlath/usher --state merged --json number,title,mergedAt`
rather than trusting the count.

- **The bounded worker pool** — ADR-0037, PR #4, merged **2026-08-13**, one
  minute after M9's own PR #3. `JobWorker` takes a scope *factory*, so there is
  one `UnitOfWork`, one event buffer and one source resolver **per job**, and
  `KIND_CONCURRENCY` (`composition.py:852`) is a table over every `JobKind`
  resolved against `USHER_JOB_CONCURRENCY` (`config.py:259`, default
  **`job_concurrency = 12`**). Recovery becomes a lease with a heartbeat
  (`JobQueue.touch()`, `ports/jobs.py:166`; `job_lease_seconds` 300.0 at
  `config.py:267`) instead of one `requeue_running()` at process start. **This is the entry most likely to be missed, because it explicitly
  corrects two PRD sections a plan may still be quoting:** PRD 01's concurrency
  table (which specified 8 enrichment and 4 sync workers against a loop that
  ran exactly one job at a time) and PRD 08's recovery rule. Measured against
  the live TMDb API: one worker **10.38 rps**, three workers **19.76 rps** and
  not 31. `api-telemetry-and-lanes.md`'s W1 entry is the record.
- **`m09e`/`m09f`** — `halfvec(384)` → `halfvec(1024)`, deleting every
  embedding, centroid and neighbour row (ADR-0038, 2026-08-13), then every
  `halfvec` column to `PLAIN` storage (`m09f`, same PR #22 merged 2026-08-14,
  no ADR of its own — its docstring and `search-and-embeddings.md` carry it).
- **The rating-provenance split** — `m10a` renames `titles`' three unprefixed
  rating columns and adds `imdb_average_rating`/`imdb_num_votes` beside them
  (ADR-0040, 2026-08-19; merged with #45). **Two of the three renames are a
  `tmdb_` prefix and the first is not, so "renamed to `tmdb_*`" is not
  derivable** — the mapping is `community_rating` → **`tmdb_vote_average`**,
  `vote_count` → `tmdb_vote_count`, `popularity` → `tmdb_popularity`
  (`_RENAMES` in `src/usher/db/migrations/versions/m10a_rating_provenance.py:135`, which
  also renames each column's CHECK and the matching `field_provenance` JSONB
  keys). No rating value was migrated — they were re-imported by
  `usher bootstrap --phase ratings` — and the rollback table
  `titles_rating_backup_20260819` is intact (1,272,870 rows on 2026-09-01);
  it is not dropped without the operator's say-so.
- **The console** — `web/`, React 19 + Vite, built into the image and served by
  Usher's own container at `/console` (shipped 2026-08-19, merged as PR #61 on
  2026-08-20). `mount_console` is defined at **`api/console.py:108`**, not in
  `api/app.py`, which only imports it (`:13`) and calls it (`:316`). Its gate is
  not the Python gate: `console.md` and `web/CONVENTIONS.md` are the record.
- **E1, the quality-eval harness** — `src/usher/eval/`, the `eval` extra
  (`ranx`), `usher eval`, and the eleventh and twelfth import contracts (the
  harness is a leaf; only its IR-metrics package imports `ranx`). PR #45, merged
  2026-08-21. One surface (`suggest`); E2–E4 are not planned. Three
  `fuzzy recall_at_5` bars are `pending` on #39. `evals.md` is the record.
- **The resumable watch lane** — `m10b`, ADR-0042 (PR #68, merged 2026-08-26).
  Two behaviours came from review rather than from the plan, and are the ones a
  later reader will most want to undo: `SyncRunRepository.save` is
  non-destructive (`completed` absorbs), and a resumed walk stamps its merges
  with the attempt's instant, not the original run's. ⚠️ **The code shipped and
  [issue #41](https://github.com/anirudhlath/usher/issues/41) is still OPEN**
  (checked 2026-09-02) — the merge did not close it, and the step that would is
  the operator's: a watch walk has still never been run to completion, which is
  the condition the issue was opened about. Do not read "PR #68 merged" as "#41
  delivered"; check `gh issue view 41 --repo anirudhlath/usher`.
- **`VisibilityService`, #73** — the plural half of PRD 03's demand lane
  (`services/visibility.py`, consumed by `api/deps.py`, `routers/browse.py`,
  `routers/search.py` and `services/home.py`; PR #74, merged 2026-08-27). The
  module docstring is the record.
- **Enrichment-driven shelf staleness** — PR #77, merged **2026-08-27**. The
  row cache had two invalidation triggers and enrichment was not one of them, so
  a title could be enriched and every shelf holding it kept serving the
  pre-enrichment card until its TTL expired. Closed across
  `services/rows/cache.py`, `services/enrich.py`, `api/lanes.py` and
  `composition.py`, with PRD 06 amended. `rows-and-genome.md`'s *"A row TTL is a
  bet that the catalog does not change"* entry is the record.

`alembic heads` is `m10b`, still exactly one. **Still not built, re-verified
against the tree on 2026-09-02:** no auth module and no `current_user`
(`api/deps.py:245`'s `get_default_user_id` still answers the singleton); no
scheduler; `query_expansion_enabled` is `False` (`config.py:596`); no GiST
trigram index; `curation_pool_size` defaults to 200 and is capped at 1000
(`config.py:565`); no `usher.llm.*` metric (the
`usher.llm.*` names in `adapters/llm/openai_compatible.py` are span
attributes, not a metric); `copy_records_to_table` is still on the raw driver
(`db/staging.py`).

**M8's eight deliberate boundary calls**, each stated with its reason in the
M8 plan's Scope section and in PRD 09: **`LiteLLMClient` is NOT built** — the
client is one `POST /v1/chat/completions` over the httpx stack already here,
because `base_url` *is* the provider abstraction, and litellm was priced at
**+146 MB and 29 distributions against +0 and 0** (three PRD sections naming it
since M1 are corrected rather than implemented, ADR-0027); **generation is a
job and `POST /admin/rows/regenerate` enqueues and returns 202**, because a
synchronous route would be the first request whose honest answer is *"the
upstream is down"* and would force PRD 07's RFC 9457 envelope a milestone
early; **the prompt addresses candidates by integer index**, because an index
is *bounds-checked* and a hallucinated UUID or IMDb id is not (the 3.1× token
figure is the cheap argument, not the real one); **the validator coerces before
it compares and counts five drop reasons**, two counting rows and three
counting cards; **the candidate pool degrades without an embedder and the taste
centroid only re-ranks it**, because implementing PRD 06's *"pre-filtered by
taste-centroid proximity"* literally makes curation the feature that never
fires on the shipped default; **`llm_calls` ships with a writer for every
column and carries NO `user_id`**, deliberately, because it is a cost ledger
joined to outcomes through `curated_rows.generation_id`; **no `usher.llm.*`
metric is added at all**, because PRD 10 puts spend on Postgres — the two
metrics that do ship are about whether the *validator* is eating the output;
and **nothing schedules the nightly run**, because there is no scheduler
anywhere in `src/` and building one for a single job would be a second
milestone inside this one.

**M8's live verification ran 2026-08-07 and produced three refutations, and
the one that matters is a product finding rather than a boundary call.** Every
boundary call held. **52 of 59 generated headings (88%) were genre labels,
which the prompt explicitly forbids**, and one heading in 59 named a
filmmaker — so on `gemma-4-26b-a4b` the curated shelf is substantively what
`GenreAffinityProvider` already produces from a `SELECT`, for free. ⚠️ One
model, one evening; the percentage transfers to nothing and **what transfers is
that the prompt's grouping instruction is not self-enforcing and nothing in
this system checks it.** The other two refutations are numbers this project had
written down: **~14.6 prompt tokens a candidate was wrong** (20.4 measured
against the *shipped* prompt, whose candidate line renders genres), and
**`USHER_CURATION_POOL_SIZE`'s `le=1000` is a bound the reference endpoint
cannot serve** (600 works, 700 and 1,000 both HTTP 400, and nothing couples the
setting to `USHER_LLM_MAX_OUTPUT_TOKENS`). Full evidence, the confirmations,
and the four recorded-not-fixed limits are in
`.claude/rules/curation-and-llm.md`; the product half is in PRD 06 and the
measurements are in ADR-0028.

**M7's nine deliberate boundary calls**, each stated with its reason in the
M7 plan's Scope section and in PRD 09: **`GET /home` IS built** (the first
client-facing route since M5, because ADR-0006's *"one request paints a
screen"* is a property of a request boundary no CLI can exhibit); **the
`curated_rows`/`LLMRow`/`CuratedProvider` family is M8's whole** and
`RowFamily` ships with two members rather than a `CURATED` nobody can emit;
**`RowCard` carries no artwork field**, absent rather than null, the same call
`GET /titles/{id}` made for `images` — ✅ **discharged by M9 Task C6 on
2026-08-11**, which is the outcome the call named and not a reversal: the field
is one image id chosen against the row's `display_hint`, added once C2 built the
table, C3 filled it and C4/C5 served it, so do not re-litigate the absence;
**`Person`/`Credit`/`Collection` ARE
built**, re-derived from `raw_payloads` with no second network call, minus
`Person`'s four `/person/{id}` fields; **weight class B is filled** and needs a
denormalised `titles.credit_names` because a generated column cannot reach
another table; **`title_search_names` is still not built** and M6's condition is
restated rather than renewed (M7 lands people, not aliases) — ✅ **discharged
by M9 Task M1 on 2026-08-11**, which is the outcome the call named and not a
reversal: `m09a` shipped the table (`src/usher/db/models/search.py`), Track 2's
akas import fills it, and the two-tier suggest reads it
(`adapters/search/prefix.py`, `api/routers/search.py`), so do not re-litigate
the absence; **the tag genome
IS built** as one dense `halfvec(1128)` per title rather than a tall table;
**rows build sequentially** because `AsyncSession` is not concurrency-safe;
and **row provider enable/disable does not become a table**, because its only
writer would be an M9 route.

**ADR-0002's typo-tolerance gate ran on 2026-08-03 against a real
1,271,138-title catalog and FAILED**, on both halves of a bar written down
before the numbers were known. The shipped type-ahead finds the right title
**27.8% of the time for a 2–4-character name** and **68.3% for 5–7**, against
bars of 0.75 and 0.85; **transposition on a 2–4-character name is 0.0%**; and
no configuration under any threshold, cap or index type comes within **6×** of
a 50 ms as-you-type budget. **Above 8 characters it is 95–100% and needs
nothing**, which is 91% of the catalog by row count. **M6 adds no Meilisearch
either way** (boundary call 7); the deliverable is the recorded failure,
ADR-0002 amended, one shipped default changed on the strength of it, and a
scoped follow-up — **the two-tier suggest, owned by M9** in PRD 09. Full
result table in `.claude/rules/search-and-embeddings.md`. *(This sentence read
"in the M6 live-verification section below" until 2026-08-07 and pointed at
nothing: this file has never had such a section, and the table has always lived
with the subsystem it measures. Live-verification evidence goes in the
subsystem rules file — M3/M4/M5's in `emby-push-and-ingest.md`, M6's in
`search-and-embeddings.md`, M8's in `curation-and-llm.md` — and this file holds
the boundary calls only.)*

**M6's nine deliberate boundary calls**, each stated with its reason in the
M6 plan's Scope section and in PRD 09: **no HTTP route** (the CLI delivers
all four capabilities; `GET /titles/{id}/similar` is M9's); **weight class B
is reserved and empty** (no `Person`/`Credit` table exists, and the only
place credits live is `raw_payloads.payload`); **no `title_search_names`
table** (with no aliases and no people it would duplicate four columns of
`titles`); **embeddings cover the enriched tier only**
(`enrichment_state <> 'skeleton'` — a skeleton needs no `index` job at all,
because its document is a generated column); **no new client event**
(`EnrichService` already publishes `title.updated`, and a second one would
have no consumer); **no query expansion** (`ports/llm.py` has no
implementation until M8); **no Meilisearch regardless of the gate**;
**similarity blends the two signals that have data** (embedding cosine plus
genre/keyword Jaccard); and **the `usher.db.staging` shared-table lock is
fixed here**, because M6's per-title `index` enqueue is what makes it hurt.

**M4's four deliberate boundary calls**, each stated with its reason in the
M4 plan's Scope section and in PRD 09: the **index** stage is M6's (no
`index` job kind ships, because a job kind whose handler is a stub is a
queue that grows forever); **push/reconnect-delta/demand/SSE** are M5's (M4
builds the queue's promotion *mechanism* but nothing calls it with
`JobPriority.DEMAND`); the **three admin HTTP routes** are M9's, with the
same capability delivered through `usher.cli`; and enrichment populates
`Title`/`Season`/`Episode` only, with `Person`/`Credit`/`Collection`/`Image`
re-derived from `raw_payloads` by M7/M9 with **no second network call**.
