---
paths:
  - "docs/plans/**"
  - "docs/prd/09-roadmap.md"
---

# What each milestone deliberately did not build

Loaded when planning or reading a milestone. Every entry names a call that a
later reader would otherwise re-litigate — each was stated with its reason in
that milestone's plan and in [PRD 09](../../docs/prd/09-roadmap.md), and is
repeated here because the plans are long and this is the part that gets lost.

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
restated rather than renewed (M7 lands people, not aliases); **the tag genome
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
