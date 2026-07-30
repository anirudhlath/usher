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

`halfvec(384)` embeddings from a local sentence-transformers model
(`bge-small-en-v1.5`) over name + overview + genres + keywords, HNSW indexed.

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
