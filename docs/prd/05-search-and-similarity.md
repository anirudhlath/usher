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

A stored generated `tsvector` with weighted fields — A: name and alternate
titles, B: cast and crew, C: overview — indexed with GIN and `fastupdate = off`
(the default buffers into a pending list that produces mysterious p99 spikes).

### Autocomplete — a separate, narrow path

**Do not route as-you-type queries through the full-text index.** Prefix
matching against a large full-text index is where the latency cliff genuinely
lives.

Instead: a narrow `(title_id, name, kind, popularity)` table over names and
aliases, GiST trigram index, candidates capped at a few thousand, then
`levenshtein_less_equal` from the core `fuzzystrmatch` module as a re-rank over
that capped set, ordered by popularity.

> 🔶 **Provisional.** This section already treats autocomplete as its own
> narrow path, separate from full-text search — but `SearchIndex.suggest`
> (`usher.ports.search`) is still one method on the same port as
> `search`/`index`/`remove`. ADR-0002 gates Meilisearch "for the
> instant-search box only", which suggests the real swap boundary may be
> `suggest` alone, not the whole class. Whether it should be its own
> `SuggestIndex` port is undecided; settle in **M6**.

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
  at that scale.
- pgvector pinned ≥ 0.8.5 (CVE-2026-3172, plus HNSW vacuum corruption fixes).

> 🔶 **Provisional.** "Brute-force exact cosine" (above) is only equivalent
> to a plain dot product if the embeddings are unit-normalised — the
> `Embedder` port (`usher.ports.embedding`) documents that contract, but
> has no query/document distinction, even though BGE-family models like
> `bge-small-en-v1.5` document a query-side instruction prefix that
> document-side text does not get. Whether the port needs
> `embed_query`/`embed_documents` instead of one `embed` is undecided;
> settle in **M6**.

### Fusion

Combine full-text and vector results with **Reciprocal Rank Fusion**, not
weighted score addition — BM25-style ranks and cosine distances are on
incompatible scales and adding them produces confident nonsense.

### Similarity

`GET /titles/{id}/similar` blends, in application code:

- embedding cosine over overview text,
- Jaccard over genres, keywords, cast, and crew,
- MovieLens tag-genome cosine where available (~7% coverage, weighted in only
  when present),
- collection membership as a strong signal.

Neighbours are precomputed offline into a `title_neighbors` table — item vectors
are static, so this is a cheap batch artifact that makes "more like this"
instant and engine-independent.

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

> 🔶 **Provisional.** The port's current shape is closer to Postgres's own
> operations than a neutral candidate-generation contract:
> `index(title_id)` forces a Meilisearch implementation to fetch each
> title back out to build its document (1.3M round-trips on a full
> rebuild); `SearchRequest.filters` has no key vocabulary, so two backends
> would invent different ones; there is no `index_many`/`rebuild` for bulk
> operations; and semantic search needs the query *vector* itself, which
> the paragraph below already anticipates supplying to Meilisearch as
> `userProvided`. Settle if and when the gate below actually trips, in
> **M6**.

**The gate is measurable, not a judgement call.** Build a typo test set from
real catalog titles, weighted toward short names — `Up`, `Her`, `Dune`, `Alien`
— where trigram similarity is genuinely weak (a four-character word yields ~5
trigrams; one typo destroys most of them, and transpositions are close to a
blind spot). If recall@5 on that set falls below the bar after honest tuning,
add Meilisearch for the instant-search box only.

If that happens: precompute embeddings and use `userProvided`, run ≥ v1.39 (a
memory leak existed from v1.12–v1.38), configure `filterableAttributes`
granularly *before* loading documents (changing them forces a full reindex),
and hydrate hits from Postgres by ID so stale index entries are invisible.

**Typesense is ruled out** regardless: fully memory-resident with no on-disk
mode, so every restart returns HTTP 503 for 2–15 minutes while it rebuilds. The
maintainers have explicitly declined to fix this outside a 3-node cluster. That
is a poor fit for a home server that reboots for kernel and driver updates.
