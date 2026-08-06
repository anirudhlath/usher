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

**M7's nine deliberate boundary calls**, each stated with its reason in the
M7 plan's Scope section and in PRD 09: **`GET /home` IS built** (the first
client-facing route since M5, because ADR-0006's *"one request paints a
screen"* is a property of a request boundary no CLI can exhibit); **the
`curated_rows`/`LLMRow`/`CuratedProvider` family is M8's whole** and
`RowFamily` ships with two members rather than a `CURATED` nobody can emit;
**`RowCard` carries no artwork field**, absent rather than null, the same call
`GET /titles/{id}` made for `images`; **`Person`/`Credit`/`Collection` ARE
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
result table in the M6 live-verification section below.

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
