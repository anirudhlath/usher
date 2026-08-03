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
name, **B: reserved for cast and crew and empty**, C: overview and tagline,
D: genres and keywords — indexed with GIN and `fastupdate = off` (the default
buffers into a pending list that produces mysterious p99 spikes).

**Weight class B ships reserved and empty, and that is a decision rather than
an omission.** There is no `Person`, `Credit`, `Collection` or `Image` table,
domain model or port anywhere in `src/` — `ports/metadata.py` defers all four
to M7 and M9 by name, and [09](09-roadmap.md)'s M4 boundary call 2 says the
same. The only place credits physically exist is `raw_payloads.payload`, and
assembling a search document out of a *provider's* JSON shape would put a
TMDb-shaped concept in `services/`. So `SearchDocument` carries a
`credits: Sequence[str] = ()` that is always empty in M6, and the class is
**reserved rather than repurposed**: moving overview up into B would make the
weights mean something different the day credits arrive, and the whole point
of a stored generated column is that filling B later is a migration rather
than a rewrite. Boundary call 2.

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

**There is no narrow `title_search_names` table, and the trigram index goes
directly on `titles`.** This section used to specify a narrow
`(title_id, name, kind, popularity)` table "over names and aliases", and its
justification is exactly that — *aliases and people names*, one title
contributing many rows. Neither has a data source in M6 (see weight class B
above), so the table would hold exactly one row per title duplicating
`titles(id, name, kind, popularity)`: a second copy of the same data, a
second thing to keep fresh, and a new instance of precisely the staleness
problem this milestone exists to eliminate. **When M7 lands aliases and
people, the narrow table is the migration that adds them** — and at that
point it holds rows `titles` does not, which is what its justification always
was. Boundary call 3.

Stated honestly: **that call rests on a structural argument, not on a
latency measurement.** No variant was built and timed against the direct
index, because the two would answer the same query over the same 1.27M names
with the same operator class and the narrow table's only difference in M6 is
that it is a copy. The number that *is* measured is the index type below.

**The index is GIN, not the GiST this section used to specify — and only half
of that question is closed.** Measured at 300k rows: build **579 ms against
1,965 ms**, size **7,968 kB against 22 MB**, p50 lookup **9.01 ms against
21.1 ms**. Re-measured at 2.08M names, where the honest summary is that the
two answer *different questions*: on the `%` threshold path GIN is ~110×
faster (1.671 ms / 205 buffers against 182.5 ms / 31,174), builds in 7.5 s
against 23.1 s and is 69 MB against 244 MB — but **GIN has no KNN operator
class at all**, so `ORDER BY name <-> q` under it degrades to a `Seq Scan` at
3,989.9 ms where GiST answers from the index. GIN ships because the
capped-candidate path is what `PostgresSuggestIndex` is built around and a
cap is exactly what removes GIN's only exposure — collecting every match
before the top-N sort. **A path that ever genuinely needs KNN needs a GiST
index, not a tuning change**, and no plan-shape test can distinguish the two,
so the measurements carry this choice and the suite does not.

**The cap must be ordered, and an unordered one is an active bug rather than
a simplification.** A `LIMIT` with no `ORDER BY` truncates arbitrarily, which
makes *lowering* the similarity threshold make recall **worse**: measured
66.2% @0.3 → 48.5% @0.1 → 2.6% @0.05 on a 604-case typo set. Any cap is
`ORDER BY similarity(name, q) DESC` under GIN (or `ORDER BY name <-> q` under
GiST). Capping smaller does not help — GiST KNN costs 272 ms at `LIMIT 200`
against 283 ms at `LIMIT 3000`; the cost is the traversal.

`pg_trgm.similarity_threshold` stays at its 0.3 default and is set with **`SET
LOCAL`**, never a bare `SET`, which leaks onto the next checkout of a pooled
connection — verified for this GUC and for `hnsw.*`. And **never feature-detect
a contrib GUC**: `SHOW pg_trgm.similarity_threshold` raises on a backend that
has not yet run one of the library's operators, while the `SET LOCAL` succeeds
on that same cold backend.

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

`GET /titles/{id}/similar` blends, in application code:

- embedding cosine over overview text,
- Jaccard over genres, keywords, cast, and crew,
- ⏳ MovieLens tag-genome cosine where available (~7% coverage, weighted in
  only when present) — **the importer does not exist; [09](09-roadmap.md)
  assigns it to M7**, and until then the ~7% is a plan rather than a
  measurement,
- collection membership as a strong signal.

Neighbours are precomputed offline into a `title_neighbors` table — item vectors
are static, so this is a cheap batch artifact that makes "more like this"
instant and engine-independent.

**As of M6 two of those four signals have no data in `src/` and the shipped
blend is the other two**, checked against the code rather than against this
prose. Cast and crew have no `Person`/`Credit` table, model or port anywhere;
the MovieLens tag-genome importer has never been built (there is no `movielens`
bootstrap phase and no `adapters/bulk/movielens.py` — it is now owned by M7,
see [09](09-roadmap.md)); and `titles.collection_id`
is a bare nullable UUID with no table that nothing in `src/` writes. So M6 ships
**embedding cosine (0.60) plus keyword Jaccard (0.25) and genre Jaccard
(0.15)**, written as a sum of weighted terms over an explicit signal list, so
that landing a third signal is one entry and one accessor rather than a
rewritten scorer. The weights are **chosen with an argument, not measured** —
nothing in M6 measures similarity relevance — and they are constants rather than
settings, because changing one changes what "similar" means and every stored row
was written under the old meaning.

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

**Relevance enters the blend as a rank, never as a raw score.** A `ts_rank` is
around 0.06, an RRF score around 0.016–0.033 and a cosine is in [-1, 1];
adding any of those to a popularity term in [0, 1) is
[ADR-0002](decisions/0002-postgres-first-search.md)'s incompatible-scale
prohibition committed one layer up, where the SQL-side rule cannot see it. The
service reads the outcome as an *ordering* and derives `1 / (1 + rank)` from
the position, with equal index scores sharing a rank.

**An absent signal is excluded from the blend, not scored zero.**
`titles.popularity` is null for every title TMDb has never described, which is
most of the catalog, and `popularity or 0.0` would rank a title nobody measured
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

**⏳ The gate has not been run against the real catalog.** M6 built everything
it measures and the run is outstanding; Meilisearch is not added either way,
because a second stateful service bolted on at the end of a milestone is not
what a measurement with a decision attached is for (boundary call 7).

**And the gate as defined above measures the wrong half — which is itself a
finding, from a synthetic dry run over 604 single-edit typo cases on 34
genuinely short real titles planted in a 2.08M-name corpus.** Recall is
arguable and passes; latency is not and does not:

| strategy | recall@5 | transposition | p50 | p95 |
|---|---|---|---|---|
| this section as literally written | 66.2% | 34.5% | 181 ms | 241 ms |
| GIN `%` @0.1 | **93.5%** | 82.8% | 582 ms | 1,893 ms |
| GiST KNN `ORDER BY name <-> q` | **93.5%** | 82.8% | 281 ms | **342 ms** |
| btree prefix, `lower(name) text_pattern_ops` | — (no typo tolerance) | — | **0.12–1.30 ms** | 0.14–18.85 ms |

Every configuration reaching 93.5% has a p50 of 281–582 ms against an
as-you-type budget of roughly 50 ms. **So the gate must measure latency as
well as recall**, and a run reporting recall alone does not close it — this
section and [ADR-0002](decisions/0002-postgres-first-search.md) both defined
it as recall@5 only. Recall by title length under the best tuning: **2–4
characters 79.9%**, 5–6 characters 97.5%, 7+ characters 100%.

Also measured, and it makes this section's own examples concrete:
`similarity('dune','dnue') = 0.111`, `('her','hor') = 0.143`,
`('up','uo') = 0.200` — all below the 0.3 default, so `name % 'dnue'` matches
**nothing**. "Transpositions are close to a blind spot" is exact.

If that happens: precompute embeddings and use `userProvided`, run ≥ v1.39 (a
memory leak existed from v1.12–v1.38), configure `filterableAttributes`
granularly *before* loading documents (changing them forces a full reindex),
and hydrate hits from Postgres by ID so stale index entries are invisible.

**Typesense is ruled out** regardless: fully memory-resident with no on-disk
mode, so every restart returns HTTP 503 for 2–15 minutes while it rebuilds. The
maintainers have explicitly declined to fix this outside a 3-node cluster. That
is a poor fit for a home server that reboots for kernel and driver updates.
