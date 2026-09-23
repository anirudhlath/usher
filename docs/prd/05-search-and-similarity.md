# 05 — Search and similarity

## Two workloads, not one

| | Scale | Job | What it needs |
|---|---|---|---|
| **Catalog lookup** | ~1.3M skeleton titles | "Find the half-remembered title" | Fast typo-tolerant prefix match on names and people, plus facets |
| **Library experience** | ~2k–10k owned titles | Taste, similarity, curation | Rich blending — and at this scale *every* technique is cheap |

At library scale similarity is exact brute-force cosine. **No ANN index is
required for that tier.**

## Postgres-first

v1 uses PostgreSQL for all of it. The search engine is a *candidate generator*
behind a port; the ranking blend is application code regardless of engine.

## Design

### Full-text

A stored generated `tsvector` with weighted fields — A: name and original
name, **B: `credit_names`**, C: overview and tagline, D: genres and keywords —
indexed with GIN.

**The lane's ordering is not `ts_rank_cd` alone.** A title whose name *is* the
query leads, by `lower(name) = lower(btrim(query))`, ahead of the score and
ahead of the `LIMIT`; the weight classes decide everything below it. See
[Ranking](#ranking).

**Weight class B reads `titles.credit_names`**, a denormalised `text[]` of
credited names truncated to a ranking constant. It is written by the same call
that writes `credits`, in the same transaction, and is never `NULL`.

**Two writers fill it under one predicate.** `enrichment_state = 'skeleton'`
decides which owns a title, so IMDb's bulk names cover the tail the TMDb crawl
has not reached and never disagree with `credits`.

**Changing the document's expression forces a full-column rewrite and a full
re-embed** — a maintenance window, not a hot deploy. Every fingerprint moves;
no subset of the catalog keeps its old one.

### Autocomplete — a separate, narrow path

**As-you-type queries do not go through the full-text index.**

Instead: a trigram index, candidates capped at a few hundred, then
`levenshtein_less_equal` from the core `fuzzystrmatch` module as a re-rank over
that capped set.

**The trigram index is on `titles` directly, and GIN rather than GiST.** **The
two must not both exist** — with a GiST trigram index present the planner takes
it for `%` and suggest latency more than quadruples, so a path that needs KNN
must *replace* the GIN index rather than sit beside it.

**`title_search_names` is the narrow table**, `(title_id, name, kind, region,
language)`. It carries **no `primary` rows** — a canonical name is answered by
`ix_titles_name_lower_prefix` on `titles` itself — so its two members are
`alias` and `person`. There is no `popularity` column; the re-rank reads
`titles.tmdb_vote_count`.

Both halves are written:

- **Aliases come from IMDb `title.akas`**, `kind = 'alias'`; re-importing them
  leaves the person rows intact. An alias that `lower()`-equals the title's own
  name is not stored.
- **The credited-person half is written with `credits` and
  `titles.credit_names`**, from the same mapping, in the same transaction.
  `kind` is `person`; `region` and `language` are NULL. A catalog derived
  before this half existed holds no person rows until `usher derive` re-runs.

**Two `text_pattern_ops` btree indexes serve the prefix probe**, on `titles`
and on `title_search_names`.

**The candidate cap is ordered**: `ORDER BY similarity(name, q) DESC`.

`pg_trgm.similarity_threshold` stays at its 0.3 default, set per transaction
with `SET LOCAL`.

**The result is ordered by distance, then popularity, then vote count**:
`ORDER BY dist ASC, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS
LAST, id ASC`.

⚠️ **On a bootstrap-only catalog both keys are NULL on every row**, so the
ordering degenerates to insertion order. Both keys are `NULLS LAST`, so the
loss is silent. Tracked in
[#39](https://github.com/anirudhlath/usher/issues/39). The same key picks
unwatched-row candidates, where the `id` tail decides *membership* rather than
only order.

**`suggest` is its own port.** `SuggestIndex` is a separate ABC with exactly
one method and **no write path**.

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
it.** At startup the `Embedder`'s reported width is compared against the
column's; on a mismatch Usher logs once and builds no embedder — so `INDEX`
jobs go unclaimed and the catalog-lookup tier is untouched, exactly as a
deployment with no model behaves.

**The in-process runtime is `fastembed`**, with no torch. The dependency lives
behind an extra and `USHER_EMBEDDING_ENABLED` is off by default: full-text and
trigram serve the whole catalog with no model at all, so a deployment without
one is *narrowed* rather than broken.

**The embedded population is the enriched tier, not the catalog** —
`enrichment_state <> 'skeleton'`.

**Throughput is linear in tokens, not texts**, and a typical
`name + overview + genres + keywords` document is ~100–130 tokens. A tokens/s
figure sizes the *model* and never the queue: an `index` job is the model plus
a claim, three reads, a staged `COPY` and a commit, so `usher index`'s printed
estimate runs optimistic.

**Freshness is a predicate, never an inference.** `title_embeddings` records
`model_name` (runtime *and* checkpoint) and a `source_fingerprint` — the `md5`
of the exact text embedded — so "is this stale?" is one SQL query, read by the
backfill's cursor and the `usher.search.embeddings.stale` gauge. Editing an
overview moves the fingerprint and re-claims the row; swapping the runtime
moves `model_name` and re-claims every row. `usher index` reports both counters
and writes nothing; `usher index --backfill` enqueues one job per stale title
and is re-runnable at zero write cost.

⚠️ **That scheme is true within one vector width and stops there**, and the
fingerprint reaches `title_embeddings` but not `title_neighbors`, whose
`blend_fingerprint` hashes the blend's constants and not the model — so a model
swap leaves every neighbour row reading as current.

**A title whose composed document is degenerate is refused, and the refusal is
written.** A refused title gets a row with a `NULL` embedding and the
fingerprint of the degenerate text: it stops matching the stale predicate,
starts matching a separate countable one, and is re-claimed once enrichment
gives it content. The threshold is about *empty*, not *thin*.

- `hnsw.iterative_scan = relaxed_order` is set explicitly; it is off by
  default.
- **`hnsw.ef_search` is 200**, an **unfiltered-path** setting. Over-fetch and
  re-rank on the filtered path is not built.
- Owned titles skip ANN entirely; similarity over them is exact brute-force
  cosine.
- pgvector pinned ≥ 0.8.5 (CVE-2026-3172, plus HNSW vacuum corruption fixes).

**No query/document split.** `Embedder` keeps one `embed`, and callers apply no
query-side prefix.

**Normalisation is asserted, not trusted**: the embedder checks the norm on its
first batch. Queries use `halfvec_cosine_ops`/`<=>`, so normalisation affects
speed, not correctness.

### Fusion

Full-text and vector results are combined with **Reciprocal Rank Fusion**.

### Similarity

`GET /titles/{id}/similar` blends, in application code, embedding cosine over
overview text plus Jaccard over keywords and genres. Neighbours are precomputed
offline into `title_neighbors`, so "more like this" is instant and
engine-independent.

| Term | Weight | Renormalised share |
|---|---|---|
| `cosine` | 0.45 | **0.600** |
| `keywords` | 0.20 | **0.267** |
| `genres` | 0.10 | **0.133** |

The blend renormalises over the signals that are *present*, so a term absent on
a pair changes only that pair.

**The MovieLens tag genome is not a term.** Its vectors are still imported,
stored and read per pair, so `usher similar --rebuild` goes on reporting the
fraction of candidate pairs carrying a vector on **both** sides.

⚠️ **Whichever signal arrives next must not be called `tags`.** That key named
the tag genome, and a stored score records only a `blend_fingerprint`, so a
later reader could not tell which signal a row contains. The genome, if it
returns, is `genome`; a user-tag term is `user_tags`.

⏳ **Cast/crew Jaccard and collection membership are not terms.** Adding either
needs a full `usher similar --rebuild`. Unassigned.

**A pair where only one side carries a signal scores `None`, never 0.0.**

**Jaccard of two empty sets is `None`, not `0.0`.** An absent signal leaves the
numerator *and* the denominator.

**Genres and keywords are two terms rather than one Jaccard over their union.**

⚠️ **The genre Jaccard still cannot tell "these two share no genres" from "we
do not know either one's genres"** for a title whose `genres` is empty.
`usher genres --backfill` puts both sides on one vocabulary, but the empty-set
half is untouched.

**Weights are constants, not settings.** `title_neighbors.blend_fingerprint`
records which blend produced each row, so a row written under older weights is
detectable.

**The precompute is exact, not approximate.**

**This table is the one derived artefact whose freshness is not a per-row
predicate.** A title's neighbours go stale when *some other* title gets an
embedding. So it carries an oldest-row `computed_at` rather than a fingerprint,
`None` means never computed, and it is rebuilt rather than repaired.

**The rebuild is schedulable, not automatic.** The `similar.rebuild`
registration runs on `USHER_SIMILAR_REBUILD_PERIOD_HOURS` (24 h) and
`USHER_SCHEDULER_ENABLED` is `false` by default. `rebuild(resume=True)` reads a
start cursor off the artefact once per run, so an interrupted walk is not
redone from page one; the scheduled job **refuses** a `title_embeddings`
written by a model this deployment is not configured with; and `usher similar`
with no arguments prints the whole table's age and its stale count.

### Mood queries

"Movies about isolation in space" is handled by embedding the query and
searching semantically.

**Query expansion — one LLM call rewriting the query before embedding — is
built and off by default.**

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
- **Nothing is billed on a search the semantic lane cannot serve.** Expansion
  runs only when the semantic lane has coverage under the request's filters, so
  a deployment with a model and an empty `title_embeddings` buys nothing.

## Ranking

Retrieval is separated from ranking:

1. **Retrieve** candidates (full-text, vector, or both fused).
2. **Rank** in application code — relevance, popularity, owned-vs-not, watch
   state, recency, taste-centroid proximity.

Owned titles are boosted but not exclusive: searching surfaces things you don't
have, clearly marked.

**`SearchService.search` takes a household** (`user_id`) as a keyword, and
**`SearchFilters` remains a closed vocabulary with no user field**: every field
of `SearchFilters` is a flag on `usher search` and a query parameter on
`GET /search`. Nothing on the wire reports which household answered.

**Watch state is a small boost, never a demotion.** A watched episode counts
toward its series.

🔶 **Recency is provisional**: `1 / (1 + age / 25 years)` over `release_date`
where the enriched tier has one and `year` otherwise, **absent and never zero
when both are null**.

**The weights are constrained rather than chosen freely.** The non-relevance
weights sum **strictly** below half the relevance weight, so no combination of
ownership, popularity, watch state, recency and taste can displace an exact
match — 0.70 against 0.35 + 0.15 + 0.15 + 0.02 + 0.02 + 0.005.

**An exact-name match is alone at rank 0.** The lexical lane carries an
exact-name key, `SearchHit.exact_name` =
`lower(name) = lower(btrim(query))`, ordered ahead of `ts_rank_cd` *and ahead
of the `LIMIT`*, so a title whose name is a common phrase still reaches the
ranker.

What remains after it are retrieval defects rather than ranking ones: a title
that loses to a namesake, and a name that never matches itself — a name of
nothing but stop words, or one containing ` - `, which
`websearch_to_tsquery` reads as **negation**.

**The exact-name key is not tier-1 suggest's prefix key.** On tier 1 the whole
candidate set is prefix matches and popularity does the ordering.
`mode=semantic` carries no exact-name key either: that statement is handed a
vector and no text.

**Taste-centroid proximity is *read* rather than computed.** Search reads the
household's stored `user_taste` row read-only and with no staleness predicate;
a request never computes or writes a centroid.

**The stored row's `model_name` is the filter on the other side**: only title
vectors stored under the centroid's `model_name` are compared with it.

🔶 The term is `max(0, cos)` clamped into [0, 1], and **`None` — never 0.0 —
when there is no centroid or no vector under that model**. At 0.005 it cannot
overturn `owned` or `played` at any cosine gap, and it decides where the other
five have tied.

**Relevance enters the blend as a rank, never as a raw score.** The service
reads the outcome as an *ordering* and derives `1 / (1 + rank)` from the
position, with equal index scores sharing a rank.

**An absent signal is excluded from the blend, not scored zero.**
`titles.tmdb_popularity` is null for every title TMDb has never described. At
equal relevance, unknown popularity ranks above a measured zero.

**"Owned" has one definition, and both consumers cite it.** A copy the
availability sweep retracted (`available = false`) still counts, and the read
is restricted to a title's own `media_items` row (`episode_id IS NULL`). The
`owned_only` *filter* and the owned *boost* are the same predicate.

## The upgrade path

`SearchIndex` is an ABC. Adding Meilisearch means implementing it once; nothing
above the port changes.

The port is a candidate-generation contract:

- `index_many(documents)` takes a `SearchDocument` the *service* assembles from
  a `Title` it already holds.
- `SearchFilters` is a frozen dataclass with a closed vocabulary (`kinds`,
  `year_from`, `year_to`, `genres`, `owned_only`, `min_enrichment`). A backend
  that cannot express one **raises** rather than ignoring it.
- `SearchRequest.query_vector` is computed by the caller.
- **No `rebuild`.** The predicate-driven backfill rebuilds from scratch.

**Fuzzy suggest does not recover typos in short one-word names** — `Up`,
`Her`, `Dune` — where a single typo destroys most of a four-character name's
trigrams and transposition is a total blind spot. Above eight characters the
trigram path needs nothing.

**Suggest is two-tier:**

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
rule without sending the request. Tier 2 is bounded at one character only and
**the server debounces nothing**. `usher suggest --tier` defaults to `fuzzy`
and has no minimum.

🔶 **Tier 1 is a keystroke path from seven characters up and nowhere below
it.**

**The typo-tolerance gate is `usher eval suggest --full`.** It regenerates the
gate's cases from the live catalog under a fixed seed and scores both tiers
separately against pre-registered bars in `docs/evals/bars.toml`, recording
each run in the `eval` schema and in `docs/evals/ledger.jsonl`. Three
`fuzzy recall_at_5` bars stay **pending**, blocked on
[#39](https://github.com/anirudhlath/usher/issues/39).
