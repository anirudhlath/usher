# 05 — Search and similarity

## Two workloads, not one

| | Scale | Job | What it needs |
|---|---|---|---|
| **Catalog lookup** | ~1.3M skeleton titles | "Find the half-remembered title" | Fast typo-tolerant prefix match on names and people, plus facets |
| **Library experience** | ~2k–10k owned titles | Taste, similarity, curation | Rich blending — and at this scale *every* technique is cheap |

The second is where the interesting UX lives, and at that scale brute-force
exact cosine beats an index. **No ANN index is required for the tier that
matters most.**

## Postgres-first

v1 uses PostgreSQL for all of it.

- The well-known "Postgres full-text search collapses" benchmark is driven by
  *match-set cardinality*, not corpus size — it appears when a query matches
  ~1M rows, which long-text search does and title search does not.
- **Ranking blend is application code regardless of engine.** Neither
  Meilisearch nor Typesense can express `0.6·semantic + 0.2·log(popularity) +
  0.2·recency`. So the search engine is a *candidate generator*, which makes it
  swappable behind a port.
- Staying in one system removes dual-write synchronisation, ghost documents,
  reindex-on-facet-change, and a second stateful service entirely.

## Design

### Full-text

A stored generated `tsvector` with weighted fields — A: name and original
name, **B: `credit_names`**, C: overview and tagline, D: genres and keywords —
indexed with GIN and `fastupdate = off` (the default buffers into a pending
list that produces p99 spikes, and costs a query ~7.7× read amplification).

**The lane's ordering is not `ts_rank_cd` alone.** A title whose name *is* the
query leads, by `lower(name) = lower(btrim(query))`, ahead of the score and
ahead of the `LIMIT`; the weight classes decide everything below it. See
[Ranking](#ranking).

**Weight class B reads `titles.credit_names`**, a denormalised `text[]` on the
row, because a stored generated expression may reference only the current row —
`setweight(to_tsvector(…, (SELECT … FROM credits …)), 'B')` is not expressible,
and an `IMMUTABLE`-declared function that reads `credits` is accepted in
silence and then reflects credits as of whenever each row was last written. The
column is maintained by the one call that also writes `credits`, in the same
transaction, and is `NOT NULL` because `usher_array_text` is `STRICT` and one
NULL nulls the entire document.

**Two writers fill it under one predicate.** `enrichment_state = 'skeleton'`
decides which owns a title, so IMDb's bulk names cover the tail the TMDb crawl
has not reached and never disagree with `credits`.

**Changing the document's expression forces a full-column rewrite and a full
re-embed.** `CREATE OR REPLACE FUNCTION` does not recompute stored generated
values, so a migration drops the GIN index, drops the column, re-adds it and
recreates the index — a maintenance window, not a hot deploy. The assembly is
positional, so every fingerprint moves and there is no subset of the catalog
that keeps its old one.

**The immutability wrapper is narrowed on purpose.** `array_to_string` is
`STABLE`, so the natural expression is rejected; `array_to_tsvector` emits raw
unlexized elements, so a genre search silently matches nothing. What ships is
`usher_array_text`, a custom `IMMUTABLE` SQL wrapper narrowed to `text[]`, and
widening it to `anyarray` would make its immutability promise dishonest.

`SearchDocument.credits` is filled from `titles.credit_names`, which is **not**
a `Title` field: it is `credits` projected to names and truncated to a ranking
constant.

### Autocomplete — a separate, narrow path

**Do not route as-you-type queries through the full-text index.** Prefix
matching against a large full-text index is where the latency cliff lives.

Instead: a trigram index, candidates capped at a few hundred, then
`levenshtein_less_equal` from the core `fuzzystrmatch` module as a re-rank over
that capped set.

**The trigram index is on `titles` directly, and GIN rather than GiST.** GIN is
far faster on the `%` threshold path and smaller to build; GiST buys a few
points of recall at 6× the p50, which is what a keystroke pays. **The two must
not both exist** — with a GiST trigram index present the planner takes it for
`%` and the shipped p50 more than quadruples, so a path that needs KNN must
*replace* the GIN index rather than sit beside it.

**`title_search_names` is the narrow table**, `(title_id, name, kind, region,
language)`. It carries **no `primary` rows** — a canonical name is answered by
`ix_titles_name_lower_prefix` on `titles` itself — so its two members are
`alias` and `person`. `region` and `language` are not decoration: without them
a French and a Brazilian alias for the same film are indistinguishable rows.
There is no `popularity` column; the re-rank reads `titles.tmdb_vote_count`.

Both halves are written:

- **Aliases come from IMDb `title.akas`**, via
  `BulkCatalogRepository.replace_aliases` — `kind = 'alias'`, with the delete
  scoped by `imdb_ids` **and** `kind` so the people half survives it. An alias
  that `lower()`-equals the title's own name is not stored, because
  `ix_titles_name_lower_prefix` already answers it.
- **The credited-person half is written by
  `CreditRepository.replace_for_titles`** — the call that already writes
  `credits` and `titles.credit_names`, from the same mapping, in the same
  transaction, with the delete scoped by `title_id` **and** `kind`. `kind` is
  `person`; `region` and `language` are NULL. A catalog derived before this
  landed holds no rows here until `usher derive` re-runs.

**Two `text_pattern_ops` btree indexes serve the prefix probe**, on `titles`
and on `title_search_names`. The pre-existing `(lower(name), year)` index
cannot answer `LIKE 'pre%'` at all under this collation.

**The cap must be ordered.** A `LIMIT` with no `ORDER BY` truncates
arbitrarily, which makes *lowering* the similarity threshold make recall worse.
Any cap is `ORDER BY similarity(name, q) DESC`. Capping wider makes recall
worse too — a bigger pool means more equal-distance competitors.

`pg_trgm.similarity_threshold` stays at its 0.3 default and is set with **`SET
LOCAL`**, never a bare `SET`, which leaks onto the next checkout of a pooled
connection. And **never feature-detect a contrib GUC**: `SHOW
pg_trgm.similarity_threshold` raises on a backend that has not yet run one of
the library's operators, while the `SET LOCAL` succeeds on that same backend.

**The result is ordered by popularity and then by vote count**, because
popularity is sparse: `ORDER BY dist ASC, tmdb_popularity DESC NULLS LAST,
tmdb_vote_count DESC NULLS LAST, id ASC`.

⚠️ **On a bootstrap-only catalog both keys are NULL on every row**, so the
ordering degenerates to insertion order. Both keys are `NULLS LAST`, so the
loss is silent. Pointing the key at `imdb_num_votes` would restore its reach
and is a ranking change owing its own measurement —
[#39](https://github.com/anirudhlath/usher/issues/39). The same key feeds
`TitleRepository.list_unwatched_candidates`, where the `id` tail decides
*membership* rather than only order.

**`suggest` is its own port.** `SuggestIndex` is a separate ABC with exactly
one method and **no write path**, so a second engine added for the
instant-search box makes its dual-write cost visible in the type system rather
than looking like implementing a method that was already there.

### Semantic

`halfvec(1024)` embeddings over name + original name + overview + tagline +
genres + keywords, HNSW indexed, from **either** of two `Embedder` runtimes
chosen by a prefix on `USHER_EMBEDDING_MODEL`: `fastembed:<checkpoint>` loads
the model in-process behind `uv sync --extra embedding`, and
`openai:<checkpoint>` calls `POST {USHER_EMBEDDING_BASE_URL}/embeddings` on any
OpenAI-compatible server.

**The width is `halfvec`'s typmod and is deployment-wide**, shared with
`user_taste.centroid`, so changing models across widths is DDL that deletes
every stored vector, centroid and neighbour row rather than a re-embed.

**A checkpoint of the wrong width narrows the deployment rather than breaking
it.** `composition.embedder` compares the `Embedder`'s reported width against
the column's, logs once and builds no embedder — so `INDEX` jobs go unclaimed
and the catalog-lookup tier is untouched, exactly as a deployment with no model
behaves.

**The in-process runtime is `fastembed`, not sentence-transformers**, which is
59 packages and ~4.8 GiB, most of it GPU runtime pulled unconditionally.
`fastembed` has no torch, and agrees with sentence-transformers to within
1e-5 cosine on identical input. The dependency lives behind an extra and
`USHER_EMBEDDING_ENABLED` is off by default: full-text and trigram serve the
whole catalog with no model at all, so a deployment without one is *narrowed*
rather than broken.

**The embedded population is the enriched tier, not the catalog** —
`enrichment_state <> 'skeleton'`. A skeleton is a name and a year, so embedding
it produces a vector of the name, which full-text already does better and
cheaper.

**Throughput is linear in tokens, not texts**, and a realistic
`name + overview + genres + keywords` document is ~100–130 tokens. A tokens/s
figure sizes the *model* and never the queue: an `index` job is the model plus
a claim, three reads, a staged `COPY` and a commit, so `usher index`'s printed
estimate runs optimistic against a measured pass.

**Freshness is a predicate, never an inference.** `title_embeddings` records
`model_name` (runtime *and* checkpoint) and a `source_fingerprint` — the `md5`
of the exact text embedded — so "is this stale?" is one SQL query with three
consumers: the backfill's cursor, the `usher.search.embeddings.stale` gauge,
and the test that proves the enqueue-on-enrichment path closes. Editing an
overview moves the fingerprint and re-claims the row; swapping the runtime
moves `model_name` and re-claims every row. `usher index` reports both counters
and writes nothing; `usher index --backfill` enqueues one job per stale title,
keyset-paged and re-runnable at zero write cost.

⚠️ **That scheme is true within one vector width and stops there**, and the
fingerprint reaches `title_embeddings` but not `title_neighbors`, whose
`blend_fingerprint` hashes the blend's constants and not the model — so a model
swap leaves every neighbour row reading as current.

**A title whose composed document is degenerate is refused, and the refusal is
written.** Every whitespace-only input embeds to the identical vector, which is
an unbounded cluster pinned to the top of every "more like this" result rather
than a bad result. A refused title gets a row with a `NULL` embedding and the
fingerprint of the degenerate text: it stops matching the stale predicate,
starts matching a separate countable one, and is re-claimed once enrichment
gives it content. The threshold is about *empty*, not *thin*.

- `hnsw.iterative_scan = relaxed_order` **must be set explicitly** — it is off
  by default, and without it filtered vector queries suffer severe recall
  collapse.
- **`hnsw.ef_search` is 200.** Recall keeps rising past it and stops being
  affordable. Under a selective genre filter the same move buys almost
  nothing, so this is an **unfiltered-path** setting; on the filtered path the
  lever is over-fetch and re-rank, which is measured and not built.
- Owned titles skip ANN entirely; exact brute-force cosine is faster and exact
  at that scale. Store `halfvec` and convert to `float32` before any numpy dot
  product — numpy has no SIMD path for half precision and `float16` is ~140×
  slower.
- pgvector pinned ≥ 0.8.5 (CVE-2026-3172, plus HNSW vacuum corruption fixes).

**No query/document split.** `Embedder` keeps one `embed`, and callers apply no
query-side prefix: the documented BGE prefix measured as a null on one side and
significantly harmful on both.

**Normalisation is a property of the checkpoint rather than of embedders**, so
the implementation asserts the norm on its first batch rather than trusting a
model card. Two limits: after the `halfvec` cast the vectors are no longer
unit, so "cosine == dot" holds only before the cast; and the contract is
load-bearing only under the inner-product operator, while this design specifies
`halfvec_cosine_ops`/`<=>`, so normalisation buys speed here, not correctness.

### Fusion

Combine full-text and vector results with **Reciprocal Rank Fusion**, not
weighted score addition — BM25-style ranks and cosine distances are on
incompatible scales and adding them produces confident nonsense.

### Similarity

`GET /titles/{id}/similar` blends, in application code, embedding cosine over
overview text plus Jaccard over keywords and genres. Neighbours are precomputed
offline into `title_neighbors`, which makes "more like this" instant and
engine-independent.

| Term | Weight | Renormalised share |
|---|---|---|
| `cosine` | 0.45 | **0.600** |
| `keywords` | 0.20 | **0.267** |
| `genres` | 0.10 | **0.133** |

`_blend` renormalises over the signals that are *present*, so a term absent on
a pair changes only that pair.

🔴 **The MovieLens tag-genome term was built and then removed**, because the
rate that decides its weight — the fraction of candidate pairs carrying a
vector on **both** sides — measured far below the floor a 0.25 weight assumes.
The vectors are still imported, still stored and still read per pair, so the
rate goes on being reported by `usher similar --rebuild`; they are no longer a
term. `ml-latest` is movies-only and frozen, so further enrichment grows the
denominator and cannot move the numerator.

⚠️ **Whichever signal arrives next must not be called `tags`.** That key named
the tag genome, and a *user-tag* term was separately evaluated and refused
under the same word. A stored score records only a `blend_fingerprint`, so a
later reader could not tell which of the two a row contains. The genome, if it
returns, is `genome`; a user-tag term is `user_tags`.

⏳ **Cast/crew Jaccard and collection membership are not terms.** The data
exists, but `NeighborSeed`/`NeighborCandidate` carry no such field, so adding
either is a port change plus a full `usher similar --rebuild`. Unassigned.

**A pair where only one side carries a signal scores `None`, never 0.0.** Every
genome component is positive, so a real pair's cosine is well above zero and a
`0.0` would report a barely-covered catalog as fully covered.

**Jaccard of two empty sets is `None`, not `0.0`.** The naive spelling divides
by zero inside a batch job, and `0.0` is worse because it is silent: it gives
the same answer for "these two share no genres" (evidence) as for "we do not
know either one's genres" (a fact about enrichment). An absent signal leaves
the numerator *and* the denominator.

**Genres and keywords are two terms rather than one Jaccard over their union**,
because genres are a closed set of about nineteen values with two to four per
title, so genre overlap saturates, while keywords are a long tail where an
overlap of three is evidence. Merged, the genre contribution disappears inside
the keyword union.

⚠️ **`_jaccard` still cannot tell "these two share no genres" from "we do not
know either one's genres"**, which is about a title whose `genres` is empty.
`usher genres --backfill` removes the vocabulary half of this — both sides now
speak one alphabet — but the empty-set half is untouched.

**Weights are constants rather than settings**, because changing one changes
what "similar" means and every stored row was written under the old meaning —
a *detectable* condition, since `title_neighbors.blend_fingerprint` records
which blend produced each row.

**The precompute is exact, not approximate.** Recall loss in a live query is
per-query; recall loss in a cached artefact is permanent.

**This table is the one derived artefact whose freshness is not a per-row
predicate.** A title's neighbours go stale when *some other* title gets an
embedding, which nothing can decide without recomputing the row. So it carries
an oldest-row `computed_at` rather than a fingerprint, `None` means never
computed, and it is rebuilt rather than repaired.

**The rebuild is schedulable and deliberately not automatic.** The
`similar.rebuild` registration runs on `USHER_SIMILAR_REBUILD_PERIOD_HOURS`
(24 h) and `USHER_SCHEDULER_ENABLED` is `false` by default.
`rebuild(resume=True)` reads a start cursor off the artefact once per run, so
an interrupted walk is not redone from page one; the scheduled job **refuses** a
`title_embeddings` written by a model this deployment is not configured with;
and `usher similar` with no arguments prints the whole table's age and its
stale count.

### Mood queries

"Movies about isolation in space" is handled by embedding the query and
searching semantically.

**Query expansion — one LLM call rewriting the query before embedding — is
built and off by default.** Measured on this catalog it made retrieval *worse*:
the rewrites are generic critic prose that sits near the centre of a corpus of
synopses, so unrelated queries come back more alike than they went in and the
same handful of films dominates the top of each. The evidence is one model, one
small corpus and five queries, and it is the only evidence there is.

- **Where the call sits.** In front of `SearchService`'s embed, and nowhere
  else. A `full_text` search buys no completion, a deployment with no embedder
  buys none, a blank query buys none, and **`usher suggest` buys none**.
- **Only the vector is computed from the rewrite.** `SearchRequest.query` is
  still the typed string, so under RRF the lexical lane goes on matching the
  viewer's own words.
- **Off by default, behind its own setting.** `USHER_QUERY_EXPANSION_ENABLED`
  is `false` even where `USHER_LLM_ENABLED=true`:

  | `USHER_LLM_ENABLED` | `USHER_QUERY_EXPANSION_ENABLED` | |
  |---|---|---|
  | `false` | `false` | The shipped default. Every search embeds the query as typed. |
  | `true` | `false` | Curated rows, and searches embedded as typed. |
  | `true` | `true` | Adds one completion per semantic or fused search that has a model to embed with. Opt-in. |
  | `false` | `true` | **Refused at startup**, naming both variables. |
- **Reported, never silently substituted.** `SearchAnswer.expanded_query` is
  the text that was embedded, `None` when the query was embedded as typed, and
  `usher search` prints it above the results. A populated field means a
  completion was bought; an absent one means nothing about spend.
- **A failure narrows rather than fails**: an unreachable endpoint, an
  unparseable answer or a rewrite that is blank or over `MAX_QUERY_CHARS` all
  leave the search to run on the typed query. The attempt is still billed — one
  `llm_calls` row per attempted call.
- **Nothing is billed on a search the semantic lane cannot serve.** The guard
  is `SearchIndex.semantic_coverage(filters) > 0.0`, asked immediately before
  the expansion, so a deployment with a model and an empty `title_embeddings`
  buys nothing.

## Ranking

Retrieval is separated from ranking, deliberately:

1. **Retrieve** candidates (full-text, vector, or both fused).
2. **Rank** in application code — relevance, popularity, owned-vs-not, watch
   state, recency, taste-centroid proximity.

Owned titles are boosted but not exclusive: searching should surface things you
don't have, clearly marked, because that feeds discovery.

**`SearchService.search` takes a household** (`user_id`) as a keyword, and
**`SearchFilters` remains a closed vocabulary with no user field**: every field
of `SearchFilters` is a flag on `usher search` and a query parameter on
`GET /search`, so a user there would be a household any caller could name.
Nothing on the wire reports which household answered.

**Watch state is a small boost, never a demotion.** A search is overwhelmingly
a re-find intent, so demoting what the household has finished buries the film
they just typed the name of. It reads `WatchStateRepository.played_title_ids`,
which rolls a watched episode up to its series.

🔶 **Recency's constant is chosen with an argument, not measured**:
`1 / (1 + age / 25 years)` over `release_date` where the enriched tier has one
and `year` otherwise, **absent and never zero when both are null**. TMDb's
`popularity` already leans recent, so the two terms are not independent.

**The weights are constrained rather than chosen freely.** The non-relevance
weights sum **strictly** below half the relevance weight, so no combination of
ownership, popularity, watch state, recency and taste can displace an exact
match — 0.70 against 0.35 + 0.15 + 0.15 + 0.02 + 0.02 + 0.005. "Strictly" is
load-bearing: taken exactly, the equality case sums to one ulp *above* 0.70 in
IEEE-754 doubles and the property fails, so the usable interval is open and the
taste weight is its midpoint.
`test_no_combination_of_the_other_five_can_displace_an_exact_match` asserts the
arithmetic rather than an ordering.

**Rank 0 is a pure function of the lexical score, so the lexical lane carries
an exact-name key.** `SearchHit.exact_name` is
`lower(name) = lower(btrim(query))`, computed in the lexical statement and
ordered ahead of `ts_rank_cd` *and ahead of the `LIMIT`* — a title whose name
is a common phrase could otherwise fall outside the candidate window and never
reach the ranker. `SearchService._dense_ranks` reads it as the leading key, so
an exact match is **alone** at dense rank 0: `ts_rank_cd` ties are pervasive,
and a shared rank 0 cancels the relevance term and hands the decision to
popularity.

What remains after it are retrieval defects rather than ranking ones: a title
that loses to a namesake, and a name that never matches itself — a name of
nothing but stop words, or one containing ` - `, which
`websearch_to_tsquery` reads as **negation**.

**The exact-name key is deliberately not tier-1 suggest's prefix key.** On
tier 1 the whole candidate set is prefix matches and popularity does the
ordering; here the set is mixed, and a prefix key would flag every competitor
alike. `mode=semantic` carries no exact-name key either: that statement is
handed a vector and no text.

**Taste-centroid proximity is *read* rather than computed.**
`TasteService.centroid` needs an embedder and a request holds none, so routing
the term through it would ship a weight that is inert on the shipped default.
`TasteRepository.latest(user_id)` answers the household's stored `user_taste`
row read-only and with no staleness predicate, which is what stops a request
minting a row under a model it does not have.

**The stored row's `model_name` is the filter on the other side.**
`TitleEmbeddingRepository.list_for_titles` takes an optional `model_name` and
the ranking path passes the centroid's, because comparing a centroid computed
under one checkpoint against vectors stored under another is a runtime
divergence arriving as a confident cosine.

🔶 The term is `max(0, cos)` clamped into [0, 1], and **`None` — never 0.0 —
when there is no centroid or no vector under that model**. Most titles reach
this term with nothing under that model, so the absent case is the population
rather than a corner. What 0.005 can move is small: it cannot overturn `owned`
or `played` at any cosine gap, and it decides where the other five have tied.

**Relevance enters the blend as a rank, never as a raw score.** A `ts_rank` is
around 0.06, an RRF score around 0.016–0.033 and a cosine is in [-1, 1], so the
service reads the outcome as an *ordering* and derives `1 / (1 + rank)` from
the position, with equal index scores sharing a rank.

**An absent signal is excluded from the blend, not scored zero.**
`titles.tmdb_popularity` is null for every title TMDb has never described, and
`popularity or 0.0` would rank a title nobody measured identically to one
measured as unpopular. At equal relevance, unknown popularity ranks above a
measured zero.

**"Owned" has one definition, and both consumers cite it.** A copy the nightly
availability sweep retracted (`available = false`) still counts, because a
ranking that flipped when a source went down would move results for a reason
unconnected to the query; and the read is restricted to a title's own
`media_items` row (`episode_id IS NULL`). The `owned_only` *filter* and the
owned *boost* are the same predicate on purpose.

## The upgrade path

`SearchIndex` is an ABC. Adding Meilisearch means implementing it once; nothing
above the port changes.

The port is a candidate-generation contract rather than a description of
Postgres's own operations:

- `index_many(documents)` takes a `SearchDocument` the *service* assembles from
  a `Title` it already holds, so no implementation fetches a title back out.
- `SearchFilters` is a frozen dataclass with a closed vocabulary (`kinds`,
  `year_from`, `year_to`, `genres`, `owned_only`, `min_enrichment`). A backend
  that cannot express one **raises** rather than ignoring it, because an
  ignored filter returns *more* results and reads as working.
- `SearchRequest.query_vector` is computed by the caller, which is what makes
  the port engine-neutral.
- **No `rebuild`, deliberately.** It would be a second path to the same state,
  exercised only by an operator, and the predicate-driven backfill already
  rebuilds from scratch by construction.

**The typo-tolerance gate ran against the real catalog and failed.** Above
eight characters the trigram path needs nothing; **the failure is the short
one-word name** — `Up`, `Her`, `Dune` — where a single typo destroys most of a
four-character name's trigrams and transposition is a total blind spot. No
threshold, cap or index type recovers it, and the best configuration found is
still several times over a keystroke budget.

**What ships instead is a two-tier suggest**, not Meilisearch:

- **Tier 1** is `PostgresPrefixSuggestIndex` — `lower(name) LIKE 'typed%'` over
  `titles` **and** `title_search_names` as one `UNION`, so a person's name
  reaches their films from the first keystroke, ordered by the same three keys
  tier 2 uses under its distance. It reads the two `text_pattern_ops` indexes
  and **writes nothing**.
- **Tier 2** is the debounced trigram + `levenshtein_less_equal` path.

They are complements: the btree has almost no typo tolerance and the trigram
path cannot meet a keystroke budget at any setting.

**`GET /search/suggest?q=&tier=prefix|fuzzy&limit=`** is one route with two
separately-askable tiers, defaulting to `prefix`, echoing the tier that
answered, and **declining to run tier 1 below a four-character prefix** — where
the answer is `200` with no results, no query issued, and a `min_query_length`
on every response so an empty box is legible and a client can apply the same
rule without sending the request. Four is the shortest prefix at which tier 1
is cheaper than tier 2; it is deliberately not the keystroke bar, which would
leave the keystroke tier answering nothing for most of a typed word. Tier 2 is
bounded at one character only and **the server debounces nothing**.
`usher suggest --tier` defaults to `fuzzy` and has no minimum, because a
command is typed once.

🔶 **Tier 1 is a keystroke path from seven characters up and nowhere below
it.** The cost is not the sort: it is the `UNION`'s de-duplication spilling to
disk and a lossy bitmap heap scan. An *ordered* inner per-arm cap would bound
the de-duplication's input and is the first thing a follow-up should measure.

**The typo-tolerance gate is a standing measurement rather than a one-off.**
`usher eval suggest --full` regenerates the gate's cases from the live catalog
under a fixed seed and scores both tiers separately against pre-registered bars
in `docs/evals/bars.toml`, recording each run in the `eval` schema and in
`docs/evals/ledger.jsonl`. Three `fuzzy recall_at_5` bars stay **pending**,
blocked on [#39](https://github.com/anirudhlath/usher/issues/39): filling them
from an unrepaired ordering would pin a value its fix fails.

If Meilisearch is ever taken: precompute embeddings and use `userProvided`,
run ≥ v1.39 (a memory leak existed from v1.12–v1.38), configure
`filterableAttributes` granularly *before* loading documents (changing them
forces a full reindex), and hydrate hits from Postgres by ID so stale index
entries are invisible.

**Typesense is ruled out** regardless: fully memory-resident with no on-disk
mode, so every restart returns HTTP 503 for 2–15 minutes while it rebuilds.
That is a poor fit for a home server that reboots for kernel and driver
updates.
