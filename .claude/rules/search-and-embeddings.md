---
paths:
  - "src/usher/adapters/search/**"
  - "src/usher/adapters/embedding/**"
  - "src/usher/services/search.py"
  - "src/usher/services/similar.py"
  - "src/usher/services/index.py"
  - "src/usher/services/genres.py"
  - "src/usher/domain/genres.py"
  - "src/usher/db/repositories/search.py"
  - "src/usher/api/routers/search.py"
  - "scripts/measure_suggest_tiers.py"
  - "scripts/measure_exact_name_rank.py"
  - "scripts/measure_fusion_coverage_bias.py"
---

# Search, trigram, RRF and embeddings

Settled rules. Where a docstring, migration or ADR is named it holds the detail
and is more current than this file. **Spelling: `titles.popularity`,
`.vote_count`, `.community_rating` are `tmdb_popularity`, `tmdb_vote_count`,
`tmdb_vote_average` since `m10a`/ADR-0040.**

## Trigram and suggest

- **`Settings.search_trigram_threshold` stays 0.3** — 0.1 does buy recall (82.5%
  → 85.1% @5) and is refused on **latency**, 14× p50 on the one path with a
  keystroke budget. Do not restate that as "no recall": it forecloses a real
  trade. `_TRIGRAM_THRESHOLD = 0.1` in `adapters/search/postgres.py` is the
  integration driver's value; only the `Settings` value ships.
- **Keep the candidate cap at 200** — wider measures *worse*, and both the cap
  and the `levenshtein` re-rank are inert (0.0% of misses, every configuration
  ever measured). Do not centre a design on either.
- **GIN, never GiST, and never both.** GIN has no KNN operator class, so a path
  needing KNN must *replace* GIN; with both present the planner takes GiST for
  `%` at 4.3× the p50 and identical recall — and **no plan-shape test
  distinguishes them**, so a green suite is not evidence here. Keep
  `fastupdate = off`: a pending list costs 7.7× read amplification.
- **The suggest tiebreak is `tmdb_vote_count DESC NULLS LAST` under
  `tmdb_popularity`**, which stays the hard key. ⚠️ ADR-0040 moved the bootstrap
  writer to `imdb_num_votes`, so on a bootstrap-only catalog **both** are NULL on
  every row and the `ORDER BY` degenerates to `dist ASC, id ASC` — insertion
  order, tiebreak and all.

## The two-tier suggest boundary (ADR-0031)

- **`_MIN_PREFIX_CHARS` is 4 and is derived**: the shortest prefix at which tier
  1's p95 is below tier 2's. Below it tier 1 is slower than the tier it exists
  to undercut. Not a `Settings` field — it tracks catalog size.
- **Tier 1 is a prefix path, not a cheap typo path** (~2% typo recall; the
  trigram tier carries the short-name weakness, ~28% at 2–4 characters), and
  **the server debounces nothing**. Tier 1's typo cases stay on
  `TypoTolerantSuggestIndexContract`, which `PostgresPrefixSuggestIndex` does
  not sign — do not move them to the base and skip them, because **a skip reads
  as coverage and asserts nothing.**

## Full text and RRF

- **Ranking has no `LIMIT` pushdown** — a ranked query fetches every matching
  heap tuple to score it, so capping candidates is mandatory.
- **RRF has five traps and the first is silent and total.** `row_number()`
  returns `bigint`, so `1 / (60 + rank)` is integer division: every score is
  `0.0`, rows come back in `id` order, nothing errors — write `1.0 / …`. Then:
  `COALESCE` each term or a single-lane row scores `NULL` and sorts *above*
  everything; `COALESCE(ft.id, vec.id)` or it surfaces a `NULL` id; an
  `INNER JOIN` reduces hybrid search to what both lanes already agreed on; and
  **ties are pervasive**, so `id` must break ties in the outer `ORDER BY` *and*
  in each lane's `row_number()` window — only the lexical lane's pair is
  load-bearing, and dropping either half alone survives the suite.
- **`websearch_to_tsquery` reads ` - ` as negation**, so a spaced hyphen makes a
  title exclude itself, and a name of nothing but stop words yields an empty
  tsquery. Both are *retrieval* failures no ranking change reaches. Unfixed.

## Ranking: the exact-name key and the blend weights

- **Both halves of the exact-name key ship or neither works.** The SQL key
  (`lower(t.name) = lower(btrim(:query))`, ordered ahead of `score` in
  `_FULL_TEXT` and `_FUSED`'s lexical CTE) gets the row inside the lane's
  `LIMIT`; no re-weighting ranks a row the lane never returned. The service key
  (`_dense_ranks` grouping on `(exact_name, score)`) makes rank 0 a group of one,
  since a shared rank 0 cancels relevance and returns the decision to popularity.
- **Do not cap the relevance decay instead** — that makes every strong match
  displaceable by popularity and invalidates the taste ceiling below. The
  residual neither half reaches: when the competitor is *also* an exact name
  match, `exact_name DESC` ties and the fused score decides.
- **The taste weight is 0.005 and 0.01 is excluded in floating point** — at 0.01
  the challenger numerator lands one ulp above `0.70` and the sort key inverts
  regardless of id. Evaluate a boundary constant in the interpreter, not on
  paper, and read "sums below half" as strict.
- **The displacement margin at the shipped configuration is 0.004785**, not
  0.009615 — the larger figure is the bound with taste *absent*. **A bound is a
  function of which signals are present; carry the denominator with it.**
  `_blend` renormalises over present signals, and `_popularity_term` returns
  `None` rather than `0.0`, so an absent signal leaves the denominator too.

## Embeddings

- **`USHER_EMBEDDING_MODEL`'s runtime prefix is a dispatch key.** `fastembed:`
  and `openai:` select different `Embedder`s, and an unrecognised prefix raises
  at startup rather than falling back. It is part of `model_name` because two
  runtimes of one checkpoint differ by ~6× the halfvec quantisation error —
  **not interchangeable without a re-embed.**
- **The width is deployment-wide DDL (ADR-0038)**: no honest conversion between
  widths, and a change deletes every embedding, centroid and neighbour row. A
  `FakeEmbedder` must track `EMBEDDING_DIMENSIONS` (`composition.embedder`
  returns `None` on a mismatch) and **use `hashlib`, never `hash()`**.
- **`HF_HUB_OFFLINE=1` is not optional**, and its absence fails naming neither
  the network nor the cache (`RuntimeError: Cannot send a request, as the client
  has been closed`). `usher.composition` sets it with `os.environ.setdefault`
  before the library import. **Never `snapshot_download`.**
- **Throughput is linear in *tokens*, not texts — quote the invariant, never a
  texts/s rate**: ~8,000–10,700 tokens/s on CPU, ~100–130 tokens per document.
  **The backfill runs at ~15% of that** (a claim, three reads, a staged `COPY`
  and a commit per title), so `usher index`'s estimate prices the model and is
  2.5–3.3× out, and **a GPU embedder is not a backfill improvement.**
- **Every whitespace-only input embeds to the identical vector**, which would
  pin a degenerate cluster atop every "more like this". The composer refuses,
  and **a refusal is a written outcome, not a skip** (`NULL` embedding, current
  `model_name`, fingerprint of the degenerate text) — a skip leaves the row
  matching the stale predicate forever.
- **Normalisation is baked into the checkpoint** — a third module, not a library
  flag — so `FastEmbedEmbedder` asserts the norm on its first batch rather than
  trusting a model card. It stops holding after the `halfvec` cast, so
  "cosine == dot" is pre-cast only; `<=>` is normalisation-invariant, `<#>` not.
- **The BGE query prefix is a measured null and applying it to both sides is
  harmful.** The likeliest reintroduction is "fixing" `SearchService`'s
  symmetric loop to apply the documented prefix — no error, no log line. A guard
  scans *every* docstring on the port, not just the class one.

## `halfvec`, storage and HNSW

- **Store `halfvec`; convert to `float32` before any numpy dot product** —
  numpy `float16` is 140× slower, having no SIMD GEMM path.
- **All `halfvec` columns are `PLAIN` since `m09f`.** At `EXTERNAL` a 1024-lane
  value crosses `TOAST_TUPLE_THRESHOLD` and every scan becomes a TOAST descent
  per row per seed (6× on `nearest_for`). `PLAIN` caps `EMBEDDING_DIMENSIONS` at
  ~4,000 lanes; wider needs `MAIN`, unmeasured.
- **`Settings.search_hnsw_ef_search` is 200** (recall@10 is monotone in it; 400
  and 1000 are refused on cost) and **`hnsw.iterative_scan` is `relaxed_order`**
  — with it off a filtered request for ten results frequently returns fewer,
  because HNSW visits `ef_search` candidates, the filter kills them and the scan
  ends. It still emits exact distance order only while rows requested ≤
  `ef_search`, and `_FUSED`'s lanes ask for more, so which candidates reach RRF
  is approximate.
- **Set both with `SET LOCAL` and never feature-detect them**: `pg_settings`
  returns zero `hnsw.%` rows on a cold backend and rows on a warm one, so a
  probe is a flaky-test generator while `SET LOCAL` works either way. Same for
  `pg_trgm.similarity_threshold`.
- **An exact scan of `title_embeddings` in a container needs `SET LOCAL
  max_parallel_workers_per_gather = 0`** — a Parallel Hash over 1024-lane
  vectors exhausts Docker's default 64 MB `/dev/shm` mid-run.

## `nearest_for` and `usher similar --rebuild`

- **`nearest_for` forces `_EXACT_SCAN_OFF`**, so it is an exact scan per seed
  and HNSW is not involved: **91.7 ms/seed** at 1024 lanes on `PLAIN`, a ~3.3 h
  walk. Price it by driving the repository method — a hand-written
  `ORDER BY embedding <=> …` is served from the index and prices a query nobody
  runs. **A per-seed price without its population is not a price** (cost is
  linear per seed, the walk quadratic); bound a walk by seed count, never by a
  `list_embedded` prefix — UUIDv7 ids follow IMDb import order.
- **`blend_fingerprint(*, embedding_model)` hashes the model**, making a model
  swap a third cause of neighbour staleness in ADR-0020's terms;
  `SimilarityService` takes the *name*, never an `Embedder`, because a request
  must not load a model. **Zero stale is also what an empty table reports** —
  pair any such verdict with a bogus-fingerprint control.
- **The credits segment of `compose_document` is a priced compromise**:
  load-bearing for person retrieval (three orders of magnitude without it),
  harmful for plot retrieval, and the lexical lane does not cover for it —
  `search_document` weights `name` far above `credit_names`, so documentaries
  *about* a person outrank films they are in. **Read before deleting it.**
- **Stop the workers before measuring anything on the same database.** An idle
  worker burns no process CPU while holding postgres at 9–67%: every poll runs
  `SearchGauges.refresh`, an O(tier) scan that also costs ~23% of a backfill's
  wall clock. `ps` will say they are idle.

## Coverage, expansion and the optional embedder

- **`semantic_coverage`'s denominator is the enriched tier, not the catalog**,
  so it reports `1.000` where the vector lane answers for a tenth of what the
  lexical lane searches. The number is right; its *name* was wrong. **Say which
  rows are in the bottom wherever it is quoted.**
- **The embedder is optional and off by default.** `composition.worker_kinds`
  adds `JobKind.INDEX` — with its handler — only under `if embedder is not
  None`, so a worker never claims work it cannot run. `usher index` loads no
  model: staleness is a question about a recorded *name*.
- **Query expansion measured worse than the typed query and ships off**, behind
  `USHER_QUERY_EXPANSION_ENABLED` — a **second** setting, `false` even when
  `USHER_LLM_ENABLED` is true. **Only the vector comes from the rewrite**;
  `SearchRequest.query` stays typed. The rest is in `curation-and-llm.md`.
- **The coverage guard is `SearchIndex.semantic_coverage(filters)`**, on the
  same `_predicates`/`_coverage` pair as the reported number, and sits behind
  `expander is not None` — **hoisting the probe above that check is the
  tidier-looking version and the one that costs every deployment.**

## Genres

`titles.genres` unions IMDb's and TMDb's vocabularies, and two spellings of one
concept share no lexemes, so `search_document`'s weight class D and
`compose_document`'s segment 6 each saw half the catalog. `usher genres
--backfill` canonicalises through `GENRE_ALIASES` (ADR-0039). **Canonicalise
every row in Python and filter nothing in SQL** — `canonicalise_genres` also
deduplicates — and derive the affected population from `GENRE_ALIASES` itself.
