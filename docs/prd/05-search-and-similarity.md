# 05 — Search and similarity

## Two workloads, not one

Conflating these inflates the difficulty of the whole problem:

| | Scale | Job | What it needs |
|---|---|---|---|
| **Catalog lookup** | ~1.3M skeleton titles | "Find the half-remembered title" | Fast typo-tolerant prefix match on names and people, plus facets |
| **Library experience** | ~2k–10k owned titles | Taste, similarity, curation | Rich blending — and at this scale *every* technique is cheap |

The second is where the interesting UX lives, and 10k × 384 float32 is 15 MB —
brute-force exact cosine in numpy, sub-millisecond. **No ANN index is required
for the tier that matters most.**

## Postgres-first

v1 uses PostgreSQL for all of it. Full reasoning and the evidence that reversed
an earlier Meilisearch recommendation: [ADR-0002](decisions/0002-postgres-first-search.md).

Summary of why:

- The well-known "Postgres full-text search collapses" benchmark is driven by
  *match-set cardinality*, not corpus size — it appears when a query matches
  ~1M rows, which long-text search does and title search does not. The vendor
  who published it started with a 34k movie dataset and discarded it for being
  too small to show any difference against Elasticsearch.
- **Ranking blend is application code regardless of engine.** Neither
  Meilisearch nor Typesense can express `0.6·semantic + 0.2·log(popularity) +
  0.2·recency`; Meilisearch's custom ranking is only `attribute:asc|desc` as a
  bucket-sort tiebreaker. So the search engine is a *candidate generator*, which
  makes it swappable behind a port.
- Staying in one system removes dual-write synchronisation, ghost documents,
  reindex-on-facet-change, and a second stateful service entirely.

## Design

### Full-text

A stored generated `tsvector` with weighted fields — A: name and original
name, **B: `credit_names`** (M7), C: overview and tagline, D: genres and
keywords — indexed with GIN and `fastupdate = off` (the default buffers into
a pending list that produces mysterious p99 spikes).

**Weight class B is filled by M7, and the sentence M6 wrote about what that
would cost was optimistic.** M6 shipped B *reserved and empty* — correctly:
there was no `Person`, `Credit`, `Collection` or `Image` table, model or port
anywhere in `src/`, the only place credits physically existed was
`raw_payloads.payload`, and assembling a search document out of a
*provider's* JSON shape would have put a TMDb-shaped concept in `services/`.
It also wrote that *"filling B when M7 lands `Credit` is a migration rather
than a rewrite"*. **That is true only of the search path, and the migration is
a bigger one than the sentence implies.** Filling B cost three things:

- **A denormalised column, `titles.credit_names text[]`.** A stored generated
  expression may reference only the current row, so `setweight(to_tsvector(…,
  (SELECT … FROM credits …)), 'B')` is not expressible. Measured on
  PostgreSQL 17.10: the subquery form answers `ERROR: cannot use subquery in
  column generation expression` — **not** the immutability error this schema's
  wrapper trains a reader to expect, because Postgres refuses it syntactically
  before volatility is considered — and a bare cross-table reference answers
  `ERROR: missing FROM-clause entry for table "credits"`. An
  `IMMUTABLE`-declared SQL function that reads `credits` is **accepted in
  silence**, and is the worst of the three: the column it feeds then reflects
  credits as of whenever each row was last written, permanently, with no
  migration to blame. `credit_names` is maintained by the one call that also
  writes `credits`, inside the same transaction, holds the top ten billed plus
  every stored crew name, and is `NOT NULL` because `usher_array_text` is
  `STRICT` and one NULL nulls the entire document.
- **A forced full-column rewrite.** `CREATE OR REPLACE FUNCTION` does not
  recompute stored generated values, and neither does changing the expression:
  the migration drops the GIN index, drops the column, re-adds it and
  recreates the index. A table rewrite over the whole catalog — a maintenance
  window, not a hot deploy.
- **A full re-embed of the enriched tier.** The document assembly is
  positional, so an uncredited title gains a seventh *empty* segment and its
  fingerprint moves too: there is no subset of the catalog that keeps its old
  one. That is ADR-0020's scheme working, and it is 25 s to 2 min at the
  measured throughput.

`SearchDocument.credits` was carried through M6 as an always-empty parameter
so that M7 filled a caller rather than rewriting the type, and it is filled
from `titles.credit_names` — which is **not** a `Title` field: it is `credits`
projected to names and truncated to a ranking constant, so a domain model
carrying it would be a cast list that is not the cast.

**Measured class weights**, pg17.10, one term in three classes scored with
`ts_rank(…, websearch_to_tsquery('english', …))`: name **0.991** (A),
`credit_names` **0.396** (B), overview **0.198** (C) — `ts_rank`'s default
`{0.1, 0.2, 0.4, 1.0}` doing exactly what the class assignment says.

**"A stored generated `tsvector`" is right and the obvious spelling of it
does not compile — measured, not suspected.** `GENERATED ALWAYS AS (…)
STORED` rejects the natural expression with `ERROR: generation expression is
not immutable`, because `array_to_string(anyarray, text)` is `STABLE`:
`anyarray` admits element types whose output depends on a GUC (`timestamptz`
and `TimeZone`). Two further facts fall out of the same check —
`to_tsvector(regconfig, text)` *is* `IMMUTABLE`, so the explicit `'english'`
is load-bearing and a bare `to_tsvector(text)` would not work; and
`array_to_tsvector`, the obvious core-function fix, is **wrong for this
purpose**: it emits array elements as raw, unlexized, case-preserving
lexemes, so `ARRAY['Sci-Fi','Film-Noir','Drama']` stores
`'Drama' 'Film-Noir' 'Sci-Fi'` and a genre search silently matches nothing.
What ships is a custom `IMMUTABLE` SQL wrapper narrowed to `text[]`,
`usher_array_text` — and narrowing the signature is what makes the
immutability promise honest, so it must not be widened to `anyarray` to
"reuse" it.

**Changing that wrapper's body requires a forced rewrite of the column in the
same migration.** `CREATE OR REPLACE FUNCTION` does not recompute stored
generated values — verified — while a later `UPDATE` of a row *does*, which
produces a table where some rows were computed by the old definition and some
by the new with nothing to tell them apart. Migration `fa2b6c1e9d30` carries
the recipe and a test samples rows against a freshly computed document.
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md).

**`fastupdate = off` is confirmed and its real argument is the read side.**
Verified with `pageinspect`: after 5,000 inserts the default had 50 pending
pages / 5,000 pending tuples against 0 / 0. The cost that matters is what a
*query* then pays — a 1.6 MB pending list cost **231 buffers against 30, 7.7×
read amplification** on the index stage, invisible in `EXPLAIN` unless you
look at buffers. That is the mechanism behind "mysterious p99 spikes".

### Autocomplete — a separate, narrow path

**Do not route as-you-type queries through the full-text index.** Prefix
matching against a large full-text index is where the latency cliff genuinely
lives.

Instead: a trigram index, candidates capped at a few hundred, then
`levenshtein_less_equal` from the core `fuzzystrmatch` module as a re-rank over
that capped set, ordered by popularity.

**M6 built no narrow `title_search_names` table, and put the trigram index
directly on `titles`.** This section used to specify a narrow
`(title_id, name, kind, popularity)` table "over names and aliases", and its
justification is exactly that — *aliases and people names*, one title
contributing many rows. Neither had a data source in M6 (see weight class B
above), so the table would have held exactly one row per title duplicating
`titles(id, name, kind, popularity)`: a second copy of the same data, a
second thing to keep fresh, and a new instance of precisely the staleness
problem this milestone exists to eliminate. Boundary call 3.

✅ **Built by `m09a`, with five columns and not the four this section
sketches — and the paragraph above still stands.** The trigram index stays
directly on `titles`, and the narrow table duplicates nothing from it: it
carries **no `primary` rows**, because a canonical name is answered by
`ix_titles_name_lower_prefix` on `titles` itself, so a `primary` row would be
exactly the one-row-per-title copy boundary call 3 refused. Its two members
are `alias` and `person`, each with a named emitter inside M9.

`(title_id, name, kind, region, language)`. **`region` and `language` are new
and are not decoration:** IMDb `title.akas` is the alias source, and without
them a French and a Brazilian alias for the same film are indistinguishable
rows — a defect the loader cannot repair later without a second migration.

🔴 **`popularity` — the fourth column this section specified — is refused,
with a number.** `titles.popularity` is NULL on **all 1,271,138 rows**
(measured 2026-08-03), which is why the shipped suggest ordering was inert and
why the vote-count tiebreak was added. Copying a 100%-NULL column into a narrow
table is precisely the duplication boundary call 3 refused; the re-rank reads
`titles.vote_count`, as it already does. Correspondingly, *"ordered by
popularity"* in the sketch above is aspirational rather than shipped.

**Two tier-1 indexes, not one, and the pre-existing `ix_titles_name_lower_year`
is neither.** That index is `(lower(name), year)` with the *default* opclass,
which under this database's collation cannot answer `LIKE 'pre%'` at all —
measured on `pgvector/pgvector:pg17`, the plan is a `Seq Scan` even with
`enable_seqscan = off`. So `m09a` adds a btree on `lower(name)
text_pattern_ops` to **both** `titles` and `title_search_names`: p50 0.6 ms,
p95 1.0 ms, max 10 ms, 44 MB, building in 0.559 s over 1,271,138 rows, against
the trigram path's 33.3 ms p50 and 734 ms max.

⏳ **The table is empty, and both halves that fill it are still owed.** M6
deferred this to *"the day M7 lands aliases and people"*. **M7 landed people
and not aliases**, so `m09a` builds the shape and neither writer — a deferral
silently rolled forward is the exact failure [09](09-roadmap.md) names for the
tag genome (*"an obligation recorded only where it was postponed is one nobody
plans"*). Both halves, with an owner:

- **Aliases are not merely unbuilt, they are not in the cache.**
  `alternative_titles` is in neither `append_to_response` list
  ([03](03-sources-and-sync.md)), so aliases are absent from `raw_payloads`
  entirely — landing them changes the crawl's *request shape* and re-fetches
  the whole enriched tier. **Unassigned**, and named in PRD 03 rather than
  left implied by this deferral.
- **The people half belongs with M9's two-tier suggest**, which
  [ADR-0002](decisions/0002-postgres-first-search.md)'s failed gate obliges and
  which *replaces* the shipped suggest path rather than extending it. **Owner:
  M9**, together with the two-tier suggest, and it is the writer rather than
  the schema: `m09a` builds the table as part of the design that replaces the
  path, which is what removes the "redesigns against a table built for the
  design it is replacing" objection this bullet used to carry.

Stated honestly: **that call rests on a structural argument, not on a
latency measurement.** No variant was built and timed against the direct
index, because the two would answer the same query over the same 1.27M names
with the same operator class and the narrow table's only difference in M6 is
that it is a copy. The number that *is* measured is the index type below.

**The index is GIN, not the GiST this section used to specify, and that
question is now closed on real data.** Measured at 300k rows: build **579 ms
against 1,965 ms**, size **7,968 kB against 22 MB**, p50 lookup **9.01 ms
against 21.1 ms**. Re-measured at 2.08M names, where the honest summary is
that the two answer *different questions*: on the `%` threshold path GIN is
~110× faster (1.671 ms / 205 buffers against 182.5 ms / 31,174), builds in
7.5 s against 23.1 s and is 69 MB against 244 MB — but **GIN has no KNN
operator class at all**, so `ORDER BY name <-> q` under it degrades to a
`Seq Scan` at 3,989.9 ms where GiST answers from the index.

**Settled 2026-08-03 against 1,271,138 real names** by the gate below, which
ran both end to end over the same 2,993 typo cases. They trade rather than
one winning: GIN builds in **5.394 s** and occupies **75 MB** against GiST's
**11.800 s / 139 MB**, and answers at **p50 33.6 ms** against **198.1 ms**;
GiST returns **85.3%** recall@5 against **82.5%**, **47.9%** against 36.1% on
2–4-character names, and a tighter tail (**max 428 ms** against 730 ms,
because KNN traversal cost barely depends on match-set size while `%` does).
**GIN stays**: p50 is what a keystroke pays, and 2.8 points of recall do not
rescue a path already 4× over budget.

**And the two must not both exist.** With a GiST trigram index present
alongside the GIN one, the planner takes GiST for the `%` operator — the
identical shipped configuration went from p50 **33.3 ms to 141.5 ms** with
byte-identical recall. "Add GiST for a KNN path and keep GIN for `%`" is not
available; adding the second index silently taxes the first. **A path that
genuinely needs KNN needs to *replace* the GIN index, not sit beside it.** No
plan-shape test can distinguish the two, so the measurements carry this
choice and the suite does not.

**The cap must be ordered, and an unordered one is an active bug rather than
a simplification.** A `LIMIT` with no `ORDER BY` truncates arbitrarily, which
makes *lowering* the similarity threshold make recall **worse**: measured
66.2% @0.3 → 48.5% @0.1 → 2.6% @0.05 on a 604-case typo set. Any cap is
`ORDER BY similarity(name, q) DESC` under GIN (or `ORDER BY name <-> q` under
GiST). Capping smaller does not help — GiST KNN costs 272 ms at `LIMIT 200`
against 283 ms at `LIMIT 3000`; the cost is the traversal.

**The cap is a latency control and not the recall lever this section
implied.** Over the gate's 2,993 real typo cases the cap truncated **0.0%**
of the shipped configuration's misses and the `levenshtein_less_equal`
re-rank dropped **0.0%** in every configuration measured. Capping *wider*
makes recall worse, not better — GiST KNN at `LIMIT 1000` scores 83.4%
against 85.3% at `LIMIT 200` — because a bigger pool means more
equal-distance competitors for the final ordering to separate.

`pg_trgm.similarity_threshold` stays at its 0.3 default and is set with **`SET
LOCAL`**, never a bare `SET`, which leaks onto the next checkout of a pooled
connection — verified for this GUC and for `hnsw.*`. And **never feature-detect
a contrib GUC**: `SHOW pg_trgm.similarity_threshold` raises on a backend that
has not yet run one of the library's operators, while the `SET LOCAL` succeeds
on that same cold backend.

**0.3 survived the gate, and the case for lowering it does not.** At
1,271,138 real names, dropping the floor to 0.2 or 0.1 leaves recall flat or
slightly worse (82.5% @0.3 → 85.1% @0.1 *only* once the ordering below is
fixed, and 78.3% → 77.6% before it) while costing 4–14× latency (p50 33.6 ms
→ 128.7 ms → 469.2 ms). What a lower floor actually does is move a miss from
one stage to another: the gate's own diagnosis went from 63.6%
below-the-floor / 36.4% out-ranked at 0.3 to 4.0% / 71.2% at 0.1. Note that
`_TRIGRAM_THRESHOLD` in `adapters/search/postgres.py` is **0.1 and is the
*contract suite's* floor, not the shipped one** — a fixture with two rows has
no competitors, so 0.1 rescues a case there that it cannot rescue at scale.
The divergence is stated in that constant's own comment rather than left to
be discovered.

**The result is ordered by popularity *and then by vote count*, because
popularity is sparse.** `titles.popularity` is NULL on all 1,271,138 rows of
a **`--phase imdb`** catalog — the one M6's gate ran against — and on ~77% of
a **`--phase all`** one: `link_crosswalk` writes it from `tmdb_ids`, and Task
36 measured **291,584 of 1,271,570 titles (22.9%) carrying a popularity, of
which exactly 3 are 0.0** (2026-08-05, so the daily export ships real values,
not `NOT NULL DEFAULT 0` filler). So `ORDER BY dist ASC, popularity DESC NULLS
LAST, id ASC` degenerates to ordering equal-distance candidates by a UUIDv7
(insertion order) on the NULL majority, and `vote_count DESC NULLS LAST` goes
*under* popularity, is filled by the bootstrap itself (539,350 rows), and was
worth **+4.2 points of recall@5 overall and +8.3 on 2–4-character names** when
M6 shipped it 2026-08-03.

**Task 36 re-measured the ordering on the populated catalog and kept it
unchanged (2026-08-05).** Same 2,993 typo cases at seed 20260803, the
populated arm against the all-NULL one: the populated catalog costs **1.3
points overall (83.4 → 82.1)**, entirely out-ranked misses where a real
popularity promotes a wrong candidate — within the 2.0-point regression bar,
so the earlier position that a *partially* populated catalog is worse than
either extreme is **refuted**. Making `vote_count` the primary key (dropping
popularity) recovers all 1.3 points and does not hurt the all-NULL arm, but
its behaviour on a genuinely *enriched* tier could not be measured on this
skeleton catalog, so it is an M9 change to re-measure rather than a shipped
one; `NULLIF(popularity, 0)` recovers nothing, since only 3 zeros exist.

**Two numbers in this section are from different runs and must not be
subtracted from each other.** The gate table below reports **82.5%** for the
shipped configuration; Task 36's arms are **83.4% → 82.1%**. They are the same
statement over the same 2,993 cases at the same seed, measured two days apart
against two *different catalogs* — the gate's was a `--phase imdb` bootstrap of
1,271,138 titles, Task 36's a `--phase all` one of 1,271,570 — and the catalog
is the independent variable in both. The comparison that means something is
within a run (83.4 against 82.1, one arm against the other); the comparison
that does not is across them.

**And `ix_titles_popularity` was dropped in the same task** (migration `ffc`).
It was not merely unused: it was **unusable as declared** — a `DESC` btree,
which Postgres builds NULLS FIRST, while every consumer asks
`DESC NULLS LAST`, a different pathkey the planner can never satisfy from it.
`list_owned_by_tag`, added in M7 and the one statement that genuinely orders by
`titles.popularity`, plans as a Merge Semi Join over `pk_titles` and never
touches it. 9,536 kB of index that no statement could take; the migration's
docstring carries the `EXPLAIN`.

**`<%` (`word_similarity`) was measured and not taken.** It separates
fixture-scale examples better than `%` (0.8 / 0.4 / 0.2 against
0.250 / 0.250 / 0.111) and is served by the same `gin_trgm_ops` index, and
over the gate's 2,993 real cases it scores **78.1% at p50 46.1 ms** against
`%`'s 82.5% at 33.6 ms — worse on both axes. A fixture-scale separation is
not a recall figure.

> **Settled in M6 — yes, `suggest` is its own port.** This section already
> treated autocomplete as a separate narrow path while `SearchIndex.suggest`
> was one method on the same port as `search`/`index`/`remove`. It is now
> `SuggestIndex`, a separate ABC with exactly one method and **no write
> path**. The argument that decides it is dual-write visibility, not
> tidiness: if the gate below fails and Meilisearch is added for the
> instant-search box, documents must be written to *both* engines — the cost
> [ADR-0002](decisions/0002-postgres-first-search.md) refused — and splitting
> the port puts that cost in the type system rather than making it look like
> implementing a method that was already there. The shipped pair is the
> evidence: `PostgresSuggestIndex` and `PostgresSearchIndex` share a session,
> the `titles` table, and no SQL, index, GUC or ranking rule.
> [ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md).

### Semantic

`halfvec(384)` embeddings from a local `fastembed` model
(`bge-small-en-v1.5`) over name + original name + overview + tagline + genres +
keywords, HNSW indexed.

**The runtime is `fastembed`, not sentence-transformers, and this sentence is a
correction rather than a preference.** Measured 2026-08-02: sentence-transformers
is 59 packages and **4.8 GiB installed**, ~4.5 GiB of which is GPU runtime
pulled unconditionally on a host that may never have a GPU, against a `usher`
image of 332 MB. `fastembed` is 28 packages and **167 MiB**, has no torch, and
is faster on identical input (252.9 texts/s against 229.5), with min cosine
agreement **0.99999619** and top-1 identical on 205/205 documents. The
dependency lives behind an extra (`uv sync --extra embedding`) and
`USHER_EMBEDDING_ENABLED` is off by default: full-text and trigram serve all
1.27M titles with no model at all, so a deployment without it is *narrowed*
rather than broken.

**The embedded population is the enriched tier, not the catalog** —
`enrichment_state <> 'skeleton'`, for which `ix_titles_enrichment_state` is
already exactly the partial index. This is this section's own two-workload
split taken seriously: catalog lookup is full-text plus trigram over
everything, and the semantic tier is the library experience at 2k–10k titles. A
skeleton is a name and a year, so embedding it produces a vector of the name,
which full-text already does better and cheaper — and a skeleton's search
document is a generated column, so it is fully indexed with no job at all.

**Sizing, quoted as the invariant rather than as a rate** (measured 2026-08-02
on a Ryzen 7 5800X3D, CPU): throughput is linear in **tokens**, not texts, and
holds at **~8,000–10,700 tokens/s** across the whole range — 412.7 texts/s at
19 tokens, 83.5 at 100, 18.7 at 516. A realistic `name + overview + genres +
keywords` document is **~100–130 tokens**. So the enriched tier is **~25
seconds to 2 minutes**; all 1,271,138 titles would be **4–6 hours**, which is
the number the population choice avoids paying. Best CPU batch size is 16, flat
to 64, worse at 128. GPU throughput is deliberately unmeasured — the probe
found 210 MiB free of 24,564 and declined to disturb a running service.

**Freshness is a predicate, never an inference.** `title_embeddings` records
`model_name` (the runtime *and* the checkpoint, e.g.
`fastembed:BAAI/bge-small-en-v1.5`) and a `source_fingerprint` — the `md5` of
the exact text embedded — so "is this stale?" is one SQL query with three
consumers: the backfill's cursor, the `usher.search.embeddings.stale` gauge,
and the test that proves the enqueue-on-enrichment path closes. Editing a
title's overview moves the fingerprint and re-claims the row with nothing being
told; swapping the runtime moves `model_name` and re-claims every row, which is
the scheme replacing a migration. `usher index` reports both counters and
writes nothing; `usher index --backfill` enqueues one job per stale title,
keyset-paged on `titles.id` and re-runnable at zero write cost.
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md) carries
the argument and the costs.

**A title whose composed document is degenerate is refused, and the refusal is
written.** Measured: every whitespace-only input embeds to the *identical*
vector — cos = 1.0000 exactly — so a catalog of them is an unbounded cluster
pinned to the top of every "more like this" result rather than a bad result,
and no assertion about norms or dimensions can see it. A refused title
therefore gets a row with a `NULL` embedding and the fingerprint of the
degenerate text: it stops matching the stale predicate, starts matching a
separate countable one, and is re-claimed exactly once when enrichment gives it
content. Refusing by writing nothing would leave it matching the backfill
forever. The threshold is about *empty*, not *thin* — name-only skeletons
measure 0.5867 pairwise and retrieve their own enriched form at 0.7638 against
a 0.4751 cross-title mean.

- `hnsw.iterative_scan = relaxed_order` **must be set explicitly** — it is off
  by default, and without it filtered vector queries suffer severe recall
  collapse.
- Owned titles skip ANN entirely; exact brute-force cosine is faster and exact
  at that scale. **The claim above that this is "sub-millisecond" is true in
  numpy and false in Postgres**: measured at 10k vectors, 1.820 ms in
  Postgres (seq scan plus top-N) against 0.088 ms in numpy `float32`. The
  conclusion survives comfortably — 1.8 ms exact beats an approximate
  filtered HNSW scan — but **numpy `float16` is 140× slower than `float32`**
  (12.275 ms against 0.088 ms; there is no SIMD GEMM path for half
  precision), so store `halfvec` and convert to `float32` before any numpy
  dot product.
- pgvector pinned ≥ 0.8.5 (CVE-2026-3172, plus HNSW vacuum corruption fixes).
  The image used by the test suite and by compose ships **0.8.6**.

> **Settled in M6 by measurement, and both halves resolve *against* the
> previous wording.**
>
> **No query/document split.** `Embedder` keeps one `embed`. The documented
> BGE query prefix moves MRR by **−0.0028**, 95% CI `[−0.0259, +0.0203]`;
> applying it to *both* sides is significantly harmful at **−0.0663**, CI
> `[−0.1013, −0.0330]`. The experiment carries a power control — a
> deliberately wrong prefix moves MRR **−0.2497** at P(>0) = 0.000 — so this
> is a measured null rather than a blind one, and the port's old "callers are
> responsible for any query-side prefix" clause is deleted because it is the
> hazard: one symmetric loop is the cheapest way to obey it and *is* the
> −0.066 condition.
>
> **Normalisation is real, and it is a property of this checkpoint rather
> than of embedders.** Norms are 1.0 to within 5.96e-08 and the library's
> `normalize_embeddings=False` returns bit-identical vectors, because
> normalisation is a third module baked into the checkpoint; the same
> backbone without it returns norms 8.99–9.46. So the implementation asserts
> the norm on its first batch rather than trusting a model card. Two limits
> the old sentence did not carry: **after the `halfvec` cast the vectors are
> no longer unit** (norm drift 1.19e-07 → 1.21e-04), so "cosine == dot" holds
> only before the cast; and the contract is **load-bearing only under the
> inner-product operator** — `<=>` is normalisation-invariant while `<#>` is
> not, and this design specifies `halfvec_cosine_ops`/`<=>`, so normalisation
> buys speed here, not correctness.
> [ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md).

### Fusion

Combine full-text and vector results with **Reciprocal Rank Fusion**, not
weighted score addition — BM25-style ranks and cosine distances are on
incompatible scales and adding them produces confident nonsense.

### Similarity

⏳ **The route is M9's; the service and the table behind it shipped in M6.**
M6 adds no HTTP route at all (boundary call 1), so `GET /titles/{id}/similar`
does **not** exist. What exists is `SimilarityService`, the precomputed
`title_neighbors` table, and `usher similar` on the command line. M7 is the
first in-process consumer.

`GET /titles/{id}/similar` blends, in application code:

- embedding cosine over overview text,
- Jaccard over genres, keywords, cast, and crew,
- MovieLens tag-genome cosine where available, weighted in only when present.
  **The importer shipped in M7** (`usher bootstrap --phase movielens`,
  `genome_scores`), and Task 36 measured every denominator (2026-08-05, a
  `--phase all` catalog of 1,271,570 titles): 15,565 genome vectors are
  **1.22%** of all titles and **1.73%** of the 899,991 movies; **7.61%** of
  [04](04-catalog-bootstrap.md)'s "≥100 IMDb votes" priority tier (measured at
  204,494 titles) — the denominator that makes the "~7%" this line used to
  carry roughly right; and **10.68%** of a real household's 5,020 owned titles
  (10.72% of its owned movies). **The number that actually decides the term's
  weight is the candidate-pair rate** — of the 100 candidates each seed's pool
  holds, how many carry a `tags` value — measured (never squared: `coverage²`
  would say 1.14%) at **1.81%** (9,069 of 502,000 pairs). **That is far below
  the 10% floor the weight assumes**, so at 0.25 the genome reorders about one
  neighbour list in fifty-five while costing a `<=>` and a TOAST fetch on every
  candidate pair of every rebuild. **The term is kept for now, with two
  caveats and a deferral**: the 1.81% is a *conservative* floor (no TMDb key
  ran, so documents are name-shaped and the pool is name-selected, which
  weakens exactly the correlation being measured); the genome is
  **movies-only** and **frozen at 2023-07-20**, so its coverage of anything
  newer is structurally zero and decays; and the choice between a genome-aware
  candidate pool and reverting `_WEIGHTS` to M6's three signals is an **M9
  decision** to make once a genuinely enriched tier can be measured — cheap and
  detectable either way, because `title_neighbors.blend_fingerprint` records
  which blend produced each row. **M7 blends it in at weight 0.25** — see the
  four-way blend below. Two
  vectors are comparable only when they came from the same release, which is
  what `genome_scores.genome_revision` records and what
  `GenomeRepository.get_pair` refuses to blend across,
- collection membership as a strong signal.

Neighbours are precomputed offline into a `title_neighbors` table — item vectors
are static, so this is a cheap batch artifact that makes "more like this"
instant and engine-independent.

**As of M6 two of those four signals have no data in `src/` and the shipped
blend is the other two**, checked against the code rather than against this
prose. Cast and crew had no `Person`/`Credit` table, model or port anywhere at
that point (**M7 landed all three — see the note below the M7 table**);
the MovieLens tag-genome importer had never been built at that point (it
shipped in M7: `movielens` is a bootstrap phase and
`adapters/bulk/movielens.py` exists, so this signal now has data — blending it
in is M7's own similarity work, not M6's); and `titles.collection_id`
is a bare nullable UUID with no table that nothing in `src/` writes. So M6 shipped
**embedding cosine (0.60) plus keyword Jaccard (0.25) and genre Jaccard
(0.15)**, written as a sum of weighted terms over an explicit signal list.

**M7 lands the third signal, and the blend is now four terms:**

| Term | M6 | M7 | Renormalised when `tags` is absent |
|---|---|---|---|
| `cosine` | 0.60 | 0.45 | 0.45 / 0.75 = **0.600** |
| `tags` | — | **0.25** | absent |
| `keywords` | 0.25 | 0.20 | 0.20 / 0.75 = **0.267** |
| `genres` | 0.15 | 0.10 | 0.10 / 0.75 = **0.133** |

⏳ **Cast/crew Jaccard and collection membership are still not terms, and M7 is
the milestone where the distinction between "the data landed" and "the term
landed" has to be said out loud.** `people`, `credits` and `collections` are
real tables as of M7 ([02](02-data-model.md)), so the *data* both signals need
now exists — and `SimilarityService._WEIGHTS` has four keys, not six.
`NeighborSeed`/`NeighborCandidate` carry no cast, crew or collection field, so
adding either is the same port-plus-two-fakes-plus-a-surface-pin change the tag
genome turned out to be, **plus** a full `usher similar --rebuild` because
adding a term re-weights the other four and moves every stored score. It is
therefore a change with a fingerprint bump attached rather than a small one,
and it is **unassigned** — recorded here at the moment its blocker was removed,
so nobody later reads the four-signal blend as the four signals this section
specifies.

**The three carried-over weights sum to 0.75, and that is the whole argument
for these numbers rather than round ones.** `_blend` renormalises over the
signals that are *present*, so on a pair with no genome — the overwhelming
majority of them — the cosine share is **exactly 0.600, unchanged to three
decimal places**, while keywords and genres move by +0.0167 and −0.0167. Such
a pair's score therefore moves by `0.0167 × (keywords − genres)`, **bounded by
±0.0167**, and two of them can only swap if they were already within 0.033 of
each other. That is an arithmetic bound with a real residual, not a claim that
the existing ordering is preserved.

**A pair where only one side has a genome vector scores `None`, never 0.0**
([ADR-0014](decisions/0014-absence-is-not-zero.md)). This is the first site
where `0.0` is not merely uninformative but *unreachable by real data*: every
genome component is positive, so the true cosine of any real pair is well above
zero — measured floor **0.2556** over all 268,157,000 ordered off-diagonal
pairs, mean 0.6101, sd 0.0913.

**The genome term's spread was measured before its weight was chosen, and its
relevance was not.** The saturation bar was written down first — saturated if
mean ≥ 0.70, or p1 ≥ 0.50, or sd < 0.05, or the top-10 neighbour gap < 0.15 —
and no clause fired, so the vectors ship raw rather than mean-centred. That
says the term is not inert. It says nothing about whether 0.25 beats 0.20: the
weights remain **chosen with an argument, not measured**, because nothing in
this project measures similarity relevance and M7 does not change that. The two
claims are kept apart deliberately.

They stay constants rather than settings, because changing one changes what
"similar" means and every stored row was written under the old meaning — which
is now a *detectable* condition rather than a warning, since
`title_neighbors.blend_fingerprint` records which blend produced each row.

**What the third signal actually cost, because M6 published an estimate and it
was optimistic.** M6 wrote that landing a third signal is "one entry and one
accessor rather than a rewritten scorer". True of the scorer exactly — `_blend`
is untouched and no consumer of `title_neighbors` changed — but the value has to
*come from* somewhere, and the neighbour DTOs live on a **port**. The measured
bill: one `_WEIGHTS` entry, one accessor, **two port DTO fields, two widened
statements, both fakes, and the port's abstract-method pin**. The signal list
really is the extension point; the sentence understated the blast radius of a
port change, and is corrected here rather than quoted.

Genres and keywords are **two terms rather than one Jaccard over their union**,
and the reason is vocabulary size: genres are a closed set of about nineteen
values with two to four per title, so genre overlap saturates (any two dramas
score 0.33 or better regardless of subject), while keywords are a long tail
where an overlap of three is evidence. Merged, the five-element genre
contribution disappears inside a forty-element keyword union and the term
nobody weighted does all the work.

**Jaccard of two empty sets is `None`, not `0.0`.** The naive spelling divides
by zero inside a batch job — which aborts a rebuild mid-page and leaves a table
half old and half new — and `0.0` is worse because it is silent: it gives the
same answer for "these two share no genres" (evidence) as for "we do not know
either one's genres" (a fact about enrichment, not about the films). An absent
signal leaves the numerator *and* the denominator, so a thin title's neighbours
are decided by its vector rather than pushed to the bottom of every list.
[ADR-0014](decisions/0014-absence-is-not-zero.md), applied to a set-valued
field.

**The precompute is exact, not approximate**, and the argument is about the
artefact rather than about the cost: recall loss in a live query is per-query,
while recall loss in a cached artefact is permanent — a neighbour an ANN scan
missed is missed by every read of that row until the next rebuild.

**And this table is the one derived artefact whose freshness is not a per-row
predicate.** A title's neighbours go stale when *some other* title gets an
embedding, which nothing can decide without recomputing the row. So it carries
an **oldest-row `computed_at`** rather than a fingerprint, `None` means never
computed, and it is rebuilt rather than repaired. That is a weaker guarantee
than the rest of the search subsystem and is written down as weaker on purpose:
a freshness predicate that looked like the others and did not mean the same
thing would be worse than an honest gap. Nothing in M6 re-runs the rebuild —
`usher similar --rebuild` is an operator's command or a cron entry — so PRD 06's
"TTL: hours" is a statement about how long a consumer may cache what it read,
not a promise about this table's age.

### Mood queries

"Movies about isolation in space" is handled by embedding the query and
searching semantically. The cheaper, better-evidenced lever is **query
expansion**: one LLM call rewriting an emotional query into narrative language
before embedding, which measurably improves retrieval — one call per query,
rather than enriching 1.3M records.

🔴 **"Measurably improves retrieval" was the literature's claim and not this
project's, and on 2026-08-07 this project measured it and got the opposite
result.** The sentence above is kept because it is what was believed and acted
on for eight milestones; it is superseded by the run below. Query expansion is
**built and off by default behind its own setting**, and the two paragraphs
after this one are the evidence and the decision.

#### The measurement that reversed it

Run 2026-08-07 against the local vLLM serving `gemma-4-26b-a4b`. **5 mood
queries × 150 real TMDb overviews** for the 150 most-voted catalog titles,
embedded with the shipped `compose_document` and the shipped
`FastEmbedEmbedder` (`fastembed:BAAI/bge-small-en-v1.5`). **The targets were
written down before any cosine was computed.**

| | raw query | expanded |
|---|---|---|
| MRR | **0.733** | 0.373 |
| recall@10 | **0.800** | 0.533 |

The typed query wins **4 of the 5 queries** outright and ties the fifth.

**A label-free control says it is a mechanism rather than a bad draw.**
Pairwise cosine *between the five queries themselves* rises from **0.5417 to
0.5975 mean** and **0.6328 to 0.7784 max** after rewriting: five deliberately
distinct searches come back more alike than they went in. The top hit's
z-score falls in 3 of 5. The diagnosis follows from that — the rewrites are
generic critic prose (*"A dramatic exploration of profound isolation and
psychological survival…"*) which sits near the centre of a corpus of synopses,
so *Arrival*, *Seven*, *Requiem for a Dream* and *Prisoners* dominate the
expanded top-5 of **unrelated** queries.

⚠️ **The caveat is real and travels with the numbers: one model, one
150-document corpus, five queries.** It is thin evidence. It is also the *only*
evidence there is, against a claim that until now rested on the literature's
authority alone, so the default follows it. M9's `search_queries` is where a
real evaluation set — real typed queries, a full catalog, more than one model —
comes from, and it is what would reverse this back.

✅ **Built in M8, off by default, and reported rather than substituted.**
M6 declined it deliberately — `ports/llm.py` declared `LLMClient` and
`LLMPurpose.QUERY_EXPANSION` with no implementation of that port anywhere in
`src/`, and adding a second unimplemented port dependency to the search path
bought nothing M6 could measure, so **M6 embedded the query exactly as
typed** (boundary call 6). M8 supplies the implementation ([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md))
and `usher.services.query_expansion.QueryExpansionService` is the wrapper the
seam was left for.

- **Where the call sits.** In front of `SearchService`'s embed, and nowhere
  else. So a `full_text` search buys no completion, a deployment with no
  embedder buys none (there is nothing to embed), a blank query buys none (it
  is refused before the model), and **`usher suggest` buys none** — type-ahead
  has no semantic lane, which is what keeps this off the one path a client
  drives per keystroke. The unit of spend is *one search that was going to
  embed something*, exactly as curation's is one generation.
- **Only the vector is computed from the rewrite.** `SearchRequest.query` is
  still the typed string, so under RRF the lexical lane goes on matching the
  viewer's own words while the semantic lane matches the paraphrase.
- **Off by default, behind its own setting.** `USHER_QUERY_EXPANSION_ENABLED`
  is `false` — including on a deployment that has set `USHER_LLM_ENABLED=true`
  and is curating happily — so `build_pipeline` builds no expander and the
  search path is byte for byte M6's. **The two switches are independent because
  the two spenders have opposite expected values**: curation works, and
  expansion measured worse (above). M8 Task 20 shipped one switch on the
  argument that *"a second setting's only honest default is 'follow the
  first'"*; that was sound while expansion was believed to help, and the
  measurement replaces it.

  The four combinations, of which three are reachable:

  | `USHER_LLM_ENABLED` | `USHER_QUERY_EXPANSION_ENABLED` | |
  |---|---|---|
  | `false` | `false` | The shipped default. No client, no curation, no expander; every search embeds the query as typed. |
  | `true` | `false` | Curated rows, and searches embedded as typed. `usher search` opens no completion client at all. |
  | `true` | `true` | Adds one completion per semantic or fused search that has a model to embed with. Opt-in. |
  | `false` | `true` | **Refused at startup**, naming both variables. With no client there is no completion to put in front of the embed, so this would be a knob that is on and means nothing — [08](08-operations.md)'s dead-config shape. |
- **Reported, never silently substituted, and the implication runs one way.**
  `SearchAnswer.expanded_query` is the text that was embedded, `None` when the
  query was embedded as typed, and `usher search` prints it above the results.
  A viewer who searched for one thing and got results for another cannot
  otherwise tell a good expansion from a bad one, and neither can an operator
  reading their bug report. **A populated field means a completion was bought;
  an absent one means nothing about spend** — a call answering with the wrong
  key is billed in full and still leaves the field `None`.
- **A failure narrows rather than fails** ([08](08-operations.md)): an
  unreachable endpoint, an unparseable answer or a rewrite that is blank or
  over `MAX_QUERY_CHARS` all leave the search to run on the typed query. The
  attempt is still billed — one `llm_calls` row per attempted call, `ok`
  derived from `error`, `generation_id` null because this purpose produces no
  rows ([10](10-telemetry-and-dashboards.md)).
- **Measured, and it is the reason for the setting** — see the run above.
  *(This bullet read "Not measured. The retrieval improvement above is the
  literature's, not this project's" until 2026-08-07. It stopped being true the
  day the measurement ran, and the measurement pointed the other way.)*
- **Billed on searches the semantic lane cannot serve, and that is open.** The
  guard is `embedder is None`, not *"anything is embedded"* — so on a
  deployment with a model and an empty `title_embeddings` (every deployment
  before its first `usher index --backfill`), a fused search with expansion on
  buys a completion, prints `expanded: …`, and then reports
  `semantic_coverage=0.000`: **the warning arrives after the money.**
  `--mode full_text` correctly buys nothing. The correct predicate — *does any
  title in the **filtered** population have a vector* — is not answerable
  before the vector that does the filtering exists, so closing this means a new
  `TitleEmbeddingRepository` read on the search path answering a weaker
  question. Recorded rather than fixed; the default above limits it to
  deployments that opted in.

## Ranking

Retrieval is separated from ranking, deliberately:

1. **Retrieve** candidates (full-text, vector, or both fused).
2. **Rank** in application code — relevance, popularity, owned-vs-not, watch
   state, recency, taste-centroid proximity.

Owned titles are boosted but not exclusive: searching should surface things you
don't have, clearly marked, because that feeds discovery.

**Three of those six terms ship in M6 and three are M7's, each for a named
reason** (`services/search.py`). Relevance, popularity and owned-vs-not have
data behind them today. **Watch state** needs a user and `SearchRequest`
carries none — `SearchFilters` is a closed vocabulary that deliberately has no
user field, and M7 is the first milestone whose calls hold a user identity by
construction. **Recency** has data (`year` across the catalog, `release_date`
on the enriched tier) and no way to choose a decay constant: nothing in M6
measures ranking, so a half-life picked here would read like a measurement and
be a guess. There is also a double-counting argument, recorded as an argument
rather than a measurement — TMDb's `popularity` is a rolling engagement figure
and already leans recent. **Taste-centroid proximity** has no centroid; PRD 06
owns the taste model and nothing computes one.

⏳ **M7 built the centroid and wired none of the three into ranking, and
saying so is the point of this paragraph.** `TasteService` and `user_taste`
exist ([06](06-rows-and-recommendations.md)), so *"nothing computes one"* has
stopped being true — but wiring it into ranking is a `SearchService` change no
M7 task makes, and leaving the sentence above unqualified would claim a
capability that does not exist. What actually changed, term by term:

- **Taste-centroid proximity** — the centroid *table* exists and **is not a
  ranking term**; in fact `TasteService.centroid` has no caller anywhere in
  `src/` as of M7, so nothing writes `user_taste` either. Two things stand in
  the way beyond the wiring: `SearchRequest` carries no user, and on the
  request path the centroid is structurally `None`
  anyway, because it needs an embedder and the route deliberately holds none
  ([ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)).
  So a naive wiring would ship a term that is inert on the default deployment
  — the failure [06](06-rows-and-recommendations.md) already corrected once,
  for `GenreAffinityProvider`. **Owner: M9**, with the user identity the
  authentication seam owes.
- **Watch state** — still no user on `SearchRequest`. M7's calls hold a user
  identity, but they are *row* calls, not search calls; nothing narrowed the
  gap. **Owner: M9.**
- **Recency** — unchanged, and still blocked on the same thing: nothing
  measures ranking, so there is no evidence to pick a decay constant from.
  M9's `search_queries` ([10](10-telemetry-and-dashboards.md)) is what would
  supply it. **Owner: M9.**

**Relevance enters the blend as a rank, never as a raw score.** A `ts_rank` is
around 0.06, an RRF score around 0.016–0.033 and a cosine is in [-1, 1];
adding any of those to a popularity term in [0, 1) is
[ADR-0002](decisions/0002-postgres-first-search.md)'s incompatible-scale
prohibition committed one layer up, where the SQL-side rule cannot see it. The
service reads the outcome as an *ordering* and derives `1 / (1 + rank)` from
the position, with equal index scores sharing a rank.

**An absent signal is excluded from the blend, not scored zero.**
`titles.popularity` is null for every title TMDb has never described — **77.1%
of a `--phase all` catalog and 100% of a `--phase imdb` one**, measured above
rather than described as "most of it" — and `popularity or 0.0` would rank a
title nobody measured
identically to one measured as unpopular — the same rule
[ADR-0014](decisions/0014-absence-is-not-zero.md) states for watch
history, applied to a ranking term. The observable consequence: at equal
relevance, unknown popularity ranks above a measured zero.

**"Owned" has one definition, and both consumers cite it.** A copy the nightly
availability sweep retracted (`available = false`, PRD 02's soft delete) still
counts, because a ranking that flipped when a source went down would move
results for a reason unconnected to the query; and the read is restricted to a
title's own `media_items` row (`episode_id IS NULL`), which costs the bound
that a library reporting episodes but never their series row reads as not-owned
for that series. The `owned_only` *filter* and the owned *boost* are the same
predicate on purpose — two definitions is how a filtered list and a boosted
list stop agreeing.

## The upgrade path

`SearchIndex` is an ABC. Adding Meilisearch means implementing it once; nothing
above the port changes.

> **Settled in M6.** All four named defects are fixed, and the port is now a
> candidate-generation contract rather than a description of Postgres's own
> operations.
>
> - `index(title_id)` became **`index_many(documents)`** — the port takes a
>   `SearchDocument` the *service* assembles from a `Title` it already holds,
>   so no implementation ever fetches a title back out.
> - `SearchRequest.filters: dict[str, Any]` became **`SearchFilters`**, a
>   frozen dataclass with a closed vocabulary (`kinds`, `year_from`,
>   `year_to`, `genres`, `owned_only`, `min_enrichment`). A backend that
>   cannot express one **raises** rather than ignoring it, because an ignored
>   filter returns *more* results and reads as working.
> - `SearchRequest` gained **`query_vector`**, computed by the caller — which
>   is what makes the port engine-neutral and simultaneously settles who
>   applies a model's query prefix (nobody: see `### Semantic`).
> - **No `rebuild`, deliberately.** It would be a second path to the same
>   state, exercised only by an operator, and the predicate-driven backfill
>   already rebuilds from scratch by construction. A port method whose only
>   test is its own test is a liability.
>
> The fifth change is the split above: `suggest` left this port entirely
> ([ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)).

**The gate is measurable, not a judgement call.** Build a typo test set from
real catalog titles, weighted toward short names — `Up`, `Her`, `Dune`, `Alien`
— where trigram similarity is genuinely weak (a four-character word yields ~5
trigrams; one typo destroys most of them, and transpositions are close to a
blind spot). If recall@5 on that set falls below the bar after honest tuning,
add Meilisearch for the instant-search box only.

**The gate was run on 2026-08-03 against a real 1,271,138-title catalog, and
it failed.** 2,993 single-edit typo cases over 750 real movie names — five
equal-sized length bands, `vote_count ≥ 500`, 81,054 non-unique lower-cased
names excluded, four typo classes at a uniformly random position, seed
20260803 — driven through the shipped `PostgresSuggestIndex`. Full tables,
the bar as it was written down beforehand, the miss diagnosis and the
regeneration procedure are in
[ADR-0002](decisions/0002-postgres-first-search.md)'s "Evidence — the gate,
measured". The headline, per typo class and length band, for the shipped
path:

| name length | substitution | deletion | transposition | doubled letter | all | n per cell |
|---|---|---|---|---|---|---|
| 2–4 | 19.3% | 12.5% | **0.0%** | 78.7% | **27.8%** | 144–150 |
| 5–7 | 90.7% | 48.0% | 35.3% | 99.3% | **68.3%** | 150 |
| 8–11 | 99.3% | 88.7% | 94.7% | 99.3% | **95.5%** | 150 |
| 12–19 | 100.0% | 99.3% | 100.0% | 100.0% | **99.8%** | 150 |
| 20+ | 99.3% | 98.7% | 100.0% | 100.0% | **99.5%** | 150 |
| **all** | **81.7%** | **69.9%** | **66.1%** | **95.5%** | **78.3%** | 2,993 |

p50 33.3 ms, p95 208.8 ms, max 734 ms. The best configuration found under any
threshold, any cap and either index type reaches **85.3% overall and 47.9% on
the 2–4 band at p95 304 ms** — so the failure is not a tuning oversight.
**This section's own examples were exact**: `similarity('dune','dnue') = 0.111`,
`('her','hor') = 0.143`, `('up','uo') = 0.200`, all below the 0.3 default, and
transposition on a 2–4-character name measures **0.0%** — a total blind spot,
not merely a near one.

**Above 8 characters it works and needs nothing** — 95–100% at every typo
class, which is 91% of this catalog by row count. **The failure is the short
one-word name**, which is where this section always said it would be.

**M6 does not add Meilisearch** (boundary call 7): a second stateful service
bolted on at the end of a milestone is not what a measurement with a decision
attached is for. What the numbers support instead, and what
[09](09-roadmap.md) gives an owner to, is a **two-tier suggest**: a btree
`lower(name) text_pattern_ops` prefix probe on every keystroke — measured at
**p50 0.6 ms / p95 1.0 ms / max 10 ms** over the same 2,993 queries, 200–330×
faster than any fuzzy configuration and the only thing measured that fits
inside a keystroke — with the trigram + `levenshtein_less_equal` path
**debounced behind it**. They are complements: the btree has no typo
tolerance at all (1.9%) and the trigram path cannot meet a keystroke budget
at any setting.

✅ **Tier 1 is built.** `PostgresPrefixSuggestIndex`
(`adapters/search/prefix.py`) is the probe: `lower(name) LIKE 'typed%'` over
`titles` **and** `title_search_names` as one `UNION`, so a person's name
reaches their films from the first keystroke, ordered by the same three keys
tier 2 uses under its distance (`popularity DESC NULLS LAST, vote_count DESC
NULLS LAST, id ASC`) so the box does not reshuffle when the debounced tier
arrives behind it. It reads the two `text_pattern_ops` indexes `m09a` ships and
**writes nothing**, so ADR-0021's dual-write cost is still unpaid by a second
implementation of that port.

🔶 **What is measured about it here is a probe, not the shipped statement.**
The 0.6 ms figure above is a prefix probe over 1,271,138 names; the union, the
de-duplication and the sort above the `LIMIT` are not in it, and Postgres has
no `LIMIT` pushdown through a sort — so a one-character keystroke over a large
catalog is the open question. **M9's B3 measures the shipped statement at
catalog scale against a bar written before the run, and is the task authorised
to narrow the union on the strength of it.** The route that serves both tiers,
and the ADR recording the split, are B5's.

**The gate as this section defined it measured the wrong half, and that
correction stands.** A synthetic dry run over 604 cases first showed it, and
the real run confirmed the shape: recall is the half that is arguable, and a
configuration can look acceptable on recall while being 4–6× too slow for the
box it exists to serve. Both dimensions are now recorded together, per cell,
with sample sizes.

If Meilisearch is ever taken: precompute embeddings and use `userProvided`,
run ≥ v1.39 (a memory leak existed from v1.12–v1.38), configure
`filterableAttributes` granularly *before* loading documents (changing them
forces a full reindex), and hydrate hits from Postgres by ID so stale index
entries are invisible.

**Typesense is ruled out** regardless: fully memory-resident with no on-disk
mode, so every restart returns HTTP 503 for 2–15 minutes while it rebuilds. The
maintainers have explicitly declined to fix this outside a 3-node cluster. That
is a poor fit for a home server that reboots for kernel and driver updates.
