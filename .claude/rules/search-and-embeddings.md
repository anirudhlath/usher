---
paths:
  - "src/usher/adapters/search/**"
  - "src/usher/adapters/embedding/**"
  - "src/usher/services/search.py"
  - "src/usher/services/similar.py"
---

# Search, trigram, RRF and embeddings

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**M6's measurements, all taken 2026-08-02 on this host against
`pgvector/pgvector:pg17` (PostgreSQL 17.10, pgvector **0.8.6** — not the
0.8.5 the PRD floor names) unless stated otherwise, with synthetic corpora.
The exception is the gate, measured 2026-08-03 against a real catalog and
recorded immediately below.**
**ADR-0002's typo-tolerance gate: run against the real catalog on 2026-08-03,
and it failed — decisively, on both halves, and five of the seven guesses the
plan wrote down were wrong.** 1,271,138 real titles from a re-run of the M2
bootstrap; the shipped `PostgresSuggestIndex` driven from a throwaway script
outside the working tree; 2,993 single-edit typo cases over 750 real movie
names; **the test set is built from real catalog rows and is therefore not
committed — the measurement is.**
**The bar was written down before the numbers were known**: recall@5 ≥ 0.90 on
the 8+ bands, ≥ 0.85 on 5–7 (interpolated; the plan named only two bands),
≥ 0.75 on 2–4, no single typo class below 0.60 in any band, **and p95 ≤ 50 ms**
— p95 rather than p50, because a box that stutters one keystroke in twenty is
a box that stutters. Both halves, one configuration, or the gate is not
closed.

*Generation procedure, stated so it is regenerable:* movies only,
`vote_count ≥ 500`, names not unique in the catalog excluded at sampling time
(**81,054 lower-cased names are shared by more than one title**), five equal
draws of 150 over `char_length(name)` bands 2–4 / 5–7 / 8–11 / 12–19 / 20+
(eligible pools 432 / 2,532 / 7,178 / 20,520 / 17,887), four typo classes per
name at a uniformly random position, `random.Random` **seed 20260803**. Seven
two-character names admit no deletion, hence 2,993 rather than 3,000.

| name length | substitution | deletion | transposition | doubled letter | all | n per cell |
|---|---|---|---|---|---|---|
| 2–4 | 19.3% | 12.5% | **0.0%** | 78.7% | **27.8%** | 144–150 |
| 5–7 | 90.7% | 48.0% | 35.3% | 99.3% | **68.3%** | 150 |
| 8–11 | 99.3% | 88.7% | 94.7% | 99.3% | **95.5%** | 150 |
| 12–19 | 100.0% | 99.3% | 100.0% | 100.0% | **99.8%** | 150 |
| 20+ | 99.3% | 98.7% | 100.0% | 100.0% | **99.5%** | 150 |
| **all** | **81.7%** | **69.9%** | **66.1%** | **95.5%** | **78.3%** | 2,993 |

p50 **33.3 ms**, p95 **208.8 ms**, max **734 ms**; median rank 1 when found.
Every configuration measured, same 2,993 cases:

| configuration | recall@5 | 2–4 band | p50 | p95 | max |
|---|---|---|---|---|---|
| GIN `%` @0.3 cap 200 — as M6 shipped | 78.3% | 27.8% | 33.3 ms | 209 ms | 734 ms |
| GIN `%` @0.2 cap 200 | 78.3% | 32.7% | 128.7 ms | 704 ms | 989 ms |
| GIN `%` @0.1 cap 200 | 77.6% | 30.2% | 470.1 ms | 928 ms | 1,475 ms |
| **GIN `%` @0.3 cap 200 + vote tiebreak — ships now** | **82.5%** | 36.1% | **33.6 ms** | 211 ms | 730 ms |
| GIN `%` @0.1 cap 200 + vote tiebreak | 85.1% | 46.9% | 469.2 ms | 926 ms | 1,487 ms |
| GIN `<%` word_similarity @0.3 + vote tiebreak | 78.1% | 30.0% | 46.1 ms | 263 ms | 631 ms |
| GiST KNN cap 200 | 77.7% | 30.2% | 198.5 ms | 304 ms | 428 ms |
| **GiST KNN cap 200 + vote tiebreak** | **85.3%** | **47.9%** | 198.1 ms | **304 ms** | **428 ms** |
| GiST KNN cap 1000 + vote tiebreak | 83.4% | 43.8% | 201.9 ms | 311 ms | 440 ms |
| btree `lower(name) text_pattern_ops` prefix | 1.9% | 1.9% | **0.6 ms** | **1.0 ms** | **10 ms** |
**What the run refuted, refutations first.**

- **`titles.popularity` is NULL on all 1,271,138 rows, so the suggest
  statement's popularity ordering was inert and the tiebreak was `id ASC`.**
  Boundary call 4's premise is that the enriched tier is 2k–10k titles — so on
  the measured deployment "ordered by popularity" ordered by insertion order.
  The shipped code's own comment said "roughly 60% of the catalog is
  NULL-popularity skeletons"; on that catalog it is **100%**. Adding
  `vote_count DESC NULLS LAST` under popularity — a column the bootstrap fills,
  538,937 rows — is worth **+4.2 points overall and +8.3 on the 2–4 band at
  unchanged latency** and shipped with this run. This is the one shipped
  default the gate changed.

  **"Nothing in `src/` writes that column but TMDb enrichment" was part of this
  finding and is REFUTED — corrected 2026-08-03 by M6's Task 28.**
  `PostgresBulkCatalogRepository.link_crosswalk`
  (`db/repositories/bulk.py`) writes
  `popularity = COALESCE(m.popularity, t.popularity)` from `tmdb_ids`, reached
  by `usher bootstrap --phase crosswalk|all` (`cli._bootstrap`'s `crosswalk`
  arm → `BootstrapService.link_crosswalk`), and
  `BulkCatalogRepository.link_crosswalk`'s docstring documents that write.
  **Symbols rather than line numbers, since 2026-08-07**: all four citations
  here were line numbers, all four had drifted, and `cli.py:147` had drifted
  clean out of `_bootstrap` into the middle of `OPERATOR_ERRORS` — a review
  that checked it in the meantime found it *"still lands inside `_bootstrap`,
  so it is not wrong"* and left it, which is the reading a line number invites.
  Reproduced against real
  Postgres with the shipped statement run verbatim: a **skeleton** title went
  `popularity IS NULL → popularity = 0`. The gate's catalog was 100% NULL
  because it was bootstrapped `title.basics` + `title.ratings` only — **the
  IMDb phase, not `--phase all`.** M2's live run linked **291,737 of
  1,271,138** titles, so a full bootstrap leaves roughly **23%** carrying a
  popularity, most of it written onto skeleton rows. **That hypothesis — that
  the partially-populated catalog is worse than either extreme — was M7 Task
  36's headline to test, and it is now REFUTED, measured 2026-08-05** on a
  `--phase all` catalog of 1,271,570 titles. The mechanism was real (popularity
  is a *hard* key above `vote_count`), but its cost is small: **291,584 (22.9%)
  carry a popularity and exactly 3 are 0.0** — the daily export ships real
  values, not the `NOT NULL DEFAULT 0` filler the `0.0`-skeleton fear assumed —
  so the "crosswalk-linked skeleton at 0.0 outranks a 500,000-vote title" case
  is 3 rows, not a population. Re-run over M6's exact 2,993 typo cases at seed
  20260803, the populated catalog costs **1.3 points overall (83.4 → 82.1)**,
  entirely out-ranked misses, **within Task 36's 2.0-point bar** — so the
  shipped ordering is **kept unchanged**. `vote_count`-as-primary-key (dropping
  popularity) recovers all 1.3 points and does not hurt the all-NULL arm, but
  its enriched-tier behaviour is unmeasurable on a skeleton catalog and is an
  **M9** change; `NULLIF(popularity, 0)` recovers nothing (3 zeros). The
  uncorrected comment at `adapters/search/postgres.py` and
  `SearchService._popularity_term`'s "most of 1,271,138 rows" are both
  **corrected in the same task**. And the third item is sharper than "unread":
  **`ix_titles_popularity` is not merely read by nothing — it is unusable as
  declared** (a `DESC`/NULLS-FIRST btree while every consumer asks `DESC NULLS
  LAST`, a different pathkey the planner never takes; `list_owned_by_tag`, added
  in M7 Group H, *does* order by `titles.popularity` but its plan is a Merge
  Semi Join over `pk_titles` that never touches the index), and it is **dropped
  in migration `ffc`** with the full measurement in its docstring.
  **`SearchService._blend` is unaffected and was checked rather than assumed**:
  `_popularity_term` returns `None`, never `0.0`, and `_blend` drops an absent
  signal from numerator *and* denominator, so an all-NULL catalog collapses to
  relevance+owned renormalised. `SimilarityService` never reads popularity.
- **The candidate cap is not the binding constraint and the `levenshtein`
  re-rank never drops the true title.** Tracing 250 misses per configuration
  back to the stage that lost them: at the shipped configuration **63.6% fell
  below the `%` floor, 36.4% were out-ranked, 0.0% were truncated by the cap,
  0.0% were dropped by the re-rank** — and the re-rank figure is 0.0% in
  *every* configuration measured. M6's design story put the cap at the
  centre; on real data it is inert until the floor is dropped, at which point
  it becomes a new defect (24.8% at 0.1) rather than the cure.
- **Lowering the trigram floor does not convert misses into hits — it
  converts threshold-excluded misses into out-ranked ones.** 63.6%/36.4% at
  0.3 becomes 4.0%/71.2% at 0.1, recall goes 78.3% → 77.6%, and latency goes
  14×. The synthetic dry run's 66.2% → 93.5% **does not reproduce** against
  1.27M real names with real competitors. **`Settings.search_trigram_threshold`
  stays 0.3.**
- **A wider cap makes recall worse**, for the same reason: GiST KNN at
  `LIMIT 1000` scores 83.4% against 85.3% at `LIMIT 200`.
- **One-word and multi-word names of the same length behave the same** — the
  naive split (one-word 55.2%, multi-word 96.8%) looks like a huge effect and
  is pure collinearity: 99.5% of the 2–4 band is one word and 0.1% of the 20+
  band is. Held at fixed band, 8–11 is **95.9% one-word against 95.3%
  multi-word**. The length-only stratification is the right one, and the
  first reading of this number was wrong.
- **`usher index --create-indexes` does not exist.** Task 8 put the trigram
  and tsvector indexes in migration `fa2b6c1e9d30` and added both to
  `bulk.py`'s `_SUSPENDABLE_INDEXES`, so they are dropped for the bootstrap
  and rebuilt at the end of it — 10.9 s for all four, inside a 74.8 s import.
  Guess 6 was about a command the milestone did not ship.
- **`_TRIGRAM_THRESHOLD = 0.1` in `adapters/search/postgres.py` was documented
  as "the trigram floor the shipped path runs at" and is not.**
  `composition.build_pipeline` passes `Settings.search_trigram_threshold`,
  whose default is **0.3**; only the integration driver injects 0.1. So every
  typo case in `SuggestIndexContract` is green at a floor no deployment uses,
  and `test_a_high_trigram_floor_destroys_fuzzy_recall` "proves" 0.1 rescues a
  case that at 1.27M rows it does not rescue. The comment is corrected; both
  values stay, each with its measured reason.
**Confirmed, and worth the numbers.** Short names are the weak band and the
curve is monotone in length (27.8 → 68.3 → 95.5 → 99.8 → 99.5). Transposition
is the weakest class overall at 66.1% — and *within the 2–4 band it is
**0.0%***, so "close to a blind spot" was an understatement rather than an
approximation. Doubled letter is the easiest at 95.5%, as predicted. The
full-text half is unaffected and was checked rather than assumed: 15 queries ×
5 runs through the shipped `_FULL_TEXT` at 1.27M titles span **0.5–20.2 ms**,
driven entirely by match-set size (15 matches → 0.64 ms, 17,616 → 20.15 ms) —
ADR-0002's cardinality argument holding on the workload it was made for, and
the whole full-text path sitting inside the budget the type-ahead path misses.
**GIN against GiST is now decided, and the two must not both exist.** Build
over 1,271,138 names: GIN **5.394 s / 75 MB**, GiST **11.800 s / 139 MB**,
btree `lower(name) text_pattern_ops` **0.559 s / 44 MB**. GIN is 6× faster at
p50; GiST buys 2.8 points of recall, 11.8 on the short band, and a tighter
tail (max 428 ms against 734 ms, because KNN traversal cost barely depends on
match-set size). **GIN stays.** And with a GiST trigram index present
*alongside* the GIN one the planner takes GiST for `%`: the identical shipped
configuration went **33.3 ms → 141.5 ms p50 (4.3×) with byte-identical
recall**. "Add GiST for KNN and keep GIN for `%`" is not available; a path
that needs KNN must *replace* the GIN index.
**What the gate obliges.** Not Meilisearch (boundary call 7). The two-tier
suggest: btree prefix on every keystroke, the trigram path debounced behind
it. Owned by M9 in PRD 09, because a debounce and a tier split are properties
of a request boundary and M6 adds no route.
**Not settled by this run, named rather than implied:** real *typed* queries
as opposed to synthetically mutated ones (that is `search_queries`, M9's);
multi-typo queries (`_MAX_DISTANCE = 2` puts them out of reach by
construction); non-Latin scripts, where trigram extraction and word-boundary
padding behave differently and no case here tests one; the head-to-head
against Meilisearch/Typesense, which this run deliberately does not build; and
whether an *enriched* catalog changes any of it — every number here is from a
bootstrap-only catalog with `popularity` NULL throughout.
**Every whitespace-only input embeds to the *identical* vector: cos(`""`,
`" "`) = cos(`""`, `"\n"`) = 1.0000, exactly.** So a title whose composed
document is empty is not a bad result — it is a perfect unit vector at cosine
1.0 from every other empty-document title, a degenerate cluster of unbounded
size pinned to the top of every "more like this" list, invisible to any
assertion about norms, dimensions or determinism. The composer refuses, **and
a refusal is a written outcome, not a skipped one**: a `NULL` embedding, the
current `model_name`, the fingerprint of the degenerate text. Skipping it
leaves the row matching the stale predicate forever, which is **the second
time this project has hit that exact shape** — the first was the
watch-history repair refused by the very row it existed to repair. The
control that says the threshold is about *empty* and not about *thin*:
unrelated name-only skeletons measure pairwise cosine **0.5867 (sd 0.055)**,
and a skeleton retrieves its own enriched form at **0.7638** against a
**0.4751** cross-title mean — crowded, but ordered.
**`fastembed` over `sentence-transformers`, and it is not a preference.**
sentence-transformers is 59 packages, **2.62 GiB downloaded / 4.8 GiB
installed**, 104 s cold install, against a `usher` image of 332 MB — and
**~4.5 GiB of the 4.8 is GPU runtime** (`nvidia/` 2.7 G, `torch/` 1.1 G,
`triton/` 689 M) pulled unconditionally. fastembed is 28 packages, **167
MiB**, **1.2 s** cold install, no torch, and **252.9 texts/s against 229.5**
(+10%) on identical input at lower peak RSS (1,067 MiB against 1,381).
Agreement over 205 documents: **min cosine 0.99999619, top-1 identical
205/205**. Two caveats: it serves a **third-party** ONNX conversion
(`qdrant/bge-small-en-v1.5-onnx-q`), not BAAI's own weights; and the
ST↔fastembed difference (max pairwise-similarity delta **1.41e-03**) is **6×
the halfvec quantisation error**, so the two are not interchangeable without
a re-embed — which is why `model_name` records the runtime
(`fastembed:BAAI/bge-small-en-v1.5`).
**Throughput is linear in *tokens*, not texts — quote the invariant, never
the rate.** CPU holds ~8,000–10,700 tokens/s across the range: 412.7 texts/s
at 19 tokens, 83.5 at 100, 18.7 at 516. A realistic `name + overview + genres
+ keywords` document is ~100–130 tokens, i.e. **~83 texts/s** — 4–6 hours
over 1,271,138 titles, ~25 s to 2 min over the enriched tier the milestone
actually embeds. Best CPU batch size **16**, flat to 64, worse at 128.
**GPU throughput is deliberately unmeasured**: the 4090 had 210 MiB free of
24,564 (a live `vllm` container held 21,764) and the probe declined to
disturb a running service. No decision rests on a GPU number.
**The BGE query prefix is a measured null, and applying it to both sides is
harmful.** Over 210 paired observations (24 gold documents + 1,200
distractors per draw, 5 disjoint draws, 42 queries): the prefix moves MRR
**−0.0028**, 95% CI `[−0.0259, +0.0203]`. Both sides: **−0.0663**, CI
`[−0.1013, −0.0330]`. **The power control is what makes this a null rather
than a blind spot** — a deliberately wrong prefix moves MRR **−0.2497** at
P(>0) = 0.000. Corroborated twice at the library level: sentence-transformers
5.6.1's `encode_query()`/`encode_document()` and fastembed 0.8.0's
`query_embed()`/`passage_embed()` are each **bit-identical to plain
`embed()`** here, because the checkpoint declares empty prompts. **This is
the one a future contributor is most likely to reintroduce**, by "fixing"
`SearchService`'s symmetric loop to apply the documented prefix — which is
exactly the −0.066 condition, with no error and no log line to see it.
**Normalisation is baked into the checkpoint, stops holding after the
`halfvec` cast, and is not load-bearing under the operator this index uses.**
Norms are 1.0 to within **5.96e-08** and `normalize_embeddings=False` returns
**bit-identical** vectors — the flag cannot turn it off, because it is a third
module (`Transformer → Pooling → Normalize`) rather than a library step; the
same backbone with `2_Normalize` removed returns norms **8.99–9.46**, which is
why `FastEmbedEmbedder` asserts the norm on its first batch instead of
trusting a model card. After the `halfvec` cast norm drift goes 1.19e-07 →
**1.21e-04**, so "cosine == dot" holds only *before* it. And `<=>` is
normalisation-**invariant** (a norm-5 vector in the same direction gives the
identical cosine distance) while `<#>` is not — with `halfvec_cosine_ops`,
which is what ships, normalisation buys speed, not correctness.
**`HF_HUB_OFFLINE=1` is not optional, and its absence fails in a message
naming neither the network nor the cache.** With the cache warm, no network,
and the flag unset, the load raises `RuntimeError: Cannot send a request, as
the client has been closed` — huggingface_hub 1.26.0 reuses a closed client
on its retry path instead of falling back to the cache. Reproduced two
independent ways. It is also the only setting under which a genuine cache
miss produces a comprehensible `OSError`. `usher.composition` sets it with
`os.environ.setdefault` **before** the library import, driven by
`USHER_EMBEDDING_OFFLINE` (default on). And **do not use
`snapshot_download`** — 401 MB / 14 files, three redundant copies of the same
weights, against ~129–134 MB / 12 blobs on the normal path.
**`hnsw.iterative_scan` is off by default and the headline is the row count,
not the recall.** At 2% filter selectivity, recall@10 over 25 query vectors:
`off`/`ef_search=40` → recall 0.068 and **0.88 rows returned of 10**;
`off`/200 → 0.284 and 4.24; `strict_order`/40 → 0.100 and 10.00;
**`relaxed_order`/40 → 0.508 and 10.00**. With the GUC off, a request for ten
results **frequently returns zero** — `EXPLAIN` says why: `rows=1, Rows
Removed by Filter: 39`, i.e. HNSW visited `ef_search` candidates, the filter
killed them, the scan ended. **`ef_search` is the wrong lever** and
`relaxed_order` beats `strict_order` because strict terminates earlier to buy
an ordering RRF does not need. **Caveat that must travel with the numbers:**
the probe used uniform-random 384-dim vectors, the worst case for any ANN
index, so absolute recall is a pessimistic floor — **0.508 is not a
production recall figure**; what transfers is the ordering of the options and
the row-count failure. Re-run over a clustered mixture the conclusion is
unchanged and stronger (at 0.1% selectivity: default 0.3% at n=0.0,
`relaxed_order` 75.7% at n=10.0). Set it with **`SET LOCAL`** — a bare `SET`
was verified readable from a brand-new session on the same engine — and
**never feature-detect it**: `pg_settings` returns zero `hnsw.%` rows on a
cold backend and rows on a warm one, so a probe is a flaky-test generator
while the `SET LOCAL` itself succeeds either way. Same rule, separately
measured, for `pg_trgm.similarity_threshold`: `SHOW` raises on a cold
backend, a failed `SHOW` does not load the library, and `SET LOCAL` does.
**RRF has five traps and one of them is silent and total.** `row_number()`
returns **`bigint`**, so `1 / (60 + rank)` is **integer division**: every
score becomes `0.0`, the result comes back in `id` order, and **nothing
errors**. It must be `1.0 / (60 + rank)`. The other four: omitting `COALESCE`
on a term makes a single-lane row score `NULL`, and `NULLS FIRST` under
`ORDER BY … DESC` then sorts every single-lane row *above* every correctly
scored one; `COALESCE(ft.id, vec.id)` is equally mandatory or single-lane
rows surface a `NULL` id; an `INNER JOIN` reduces hybrid search to what both
lanes already agreed on; and **ties are pervasive rather than occasional** —
two disjoint 50-row lanes produced 100 fused rows with **50 distinct scores,
every one a two-way tie**, and among the top 500 `ts_rank_cd` values for one
query the largest tie group was **498**. So `id` must break ties in the outer
`ORDER BY` *and* inside each lane's `row_number()` window. SQL versus Python
is a non-question: byte-identical top-20 order on 7/7 query pairs, with
Python marginally faster; SQL wins on the single round trip and the 20-row
payload.
**The FTS collapse is caused by ranking, not by matching.** ADR-0002's
cardinality claim holds and is sharper than stated: at a constant 1.32M
corpus, latency spans **0.12 ms → 556.76 ms — a 4,600× range** — driven
entirely by match-set size. But `LIMIT 20` *without* `ORDER BY` is flat at
~0.13 ms at every cardinality, because the planner early-exits a seq scan.
Ranked, the same 650,000-match query is 601.9 ms, of which the index scan is
**42 ms** and the other **560 ms** is fetching 650,000 heap tuples so
`ts_rank_cd` can score them. **Ranking has no `LIMIT` pushdown**, so capping
candidates is mandatory rather than optional.
**Trigram: GIN, not the GiST PRD 05 specifies — closed on real data
2026-08-03, and the second half of the answer is that the two cannot
coexist.** At 300k rows GIN wins on every axis (build 579 ms vs 1,965 ms,
size 7,968 kB vs 22 MB, p50 9.01 ms vs 21.1 ms). At 2.08M names on the `%`
threshold path GIN is **~110× faster** (1.671 ms / 205 buffers against
182.5 ms / 31,174), builds in 7.5 s vs 23.1 s, and is 69 MB vs 244 MB — **but
GIN has no KNN operator class at all**, so `ORDER BY name <-> q` degrades to
a Seq Scan at **3,989.9 ms** where GiST answers from the index. The gate's
end-to-end run at 1,271,138 real names priced both: GIN **5.394 s / 75 MB /
p50 33.6 ms / recall 82.5%** against GiST KNN **11.800 s / 139 MB / p50
198.1 ms / recall 85.3%** — GIN ships, because p50 is what a keystroke pays.
And **keeping both indexes is not an option**: with GiST present the planner
takes it for `%` and the identical shipped configuration goes **33.3 ms →
141.5 ms p50 with byte-identical recall**. **No plan-shape test can
distinguish the two** — GiST serves `%` as well — so a green suite is not
evidence for this choice; the measurements are. And
`fastupdate = off`'s real argument is the read side: a 1.6 MB pending list
cost **231 buffers against 30, 7.7× read amplification**, invisible in
`EXPLAIN` unless you look at buffers.
**`halfvec(384)` is correct and effectively free, and numpy `float16` is
not.** Round-trip error over 1,000 vectors: max cosine error **1.21e-04**,
mean 3.03e-05 — three orders of magnitude below the useful signal, with top-1
and top-5 ordering identical in 42/42 queries. Storage at 1,271,138 titles:
1.83 GiB → 0.92 GiB. But brute-force exact cosine at 10k is **1.820 ms in
Postgres against 0.088 ms in numpy `float32`** — PRD 05's "sub-millisecond"
was a numpy figure — and numpy `float16` is **140× slower than `float32`**
(12.275 ms), because there is no SIMD GEMM path for half precision. Store
`halfvec`; convert to `float32` before any numpy dot product.
**The deterministic `FakeEmbedder` is `blake2b → Box-Muller → L2-normalise`,
and its non-vacuity is measured.** Over 15,996,000 off-diagonal pairs: cosine
mean −0.00001, **sd 0.05102 against a theoretical 1/√384 = 0.05103** (ratio
1.000), max +0.2549, **zero pairs above 0.5**. **Use `hashlib`, never
`hash()`** — `np.random.default_rng(abs(hash(text)))` passes *every* contract
check and fails only across processes, because `str.__hash__` is
`PYTHONHASHSEED`-salted, so the cross-process case must be pinned. A
hashing-trick TF-IDF fake was built and **rejected on evidence**: off-diagonal
cosine floor **+0.723**, and it collapses case and punctuation to 1.00000,
which is the vacuous-pass failure mode itself. For a test needing a *known*
similarity, plant the angle (`v = cos θ·a + sin θ·b`, `a ⊥ b`) — exact to
2.22e-16 — rather than hoping a hash produces one.
**A relevance assertion that any ordering satisfies is not a relevance
test**, and it is the easiest way to ship a search that does not work.
`assert title_id in {h.title_id for h in hits}` passes against an
implementation returning the whole table in physical order, and so does
`assert len(hits) > 0`. Every retrieval case in M6 asserts on **position**,
seeds a **distractor a broken implementation would rank first**, and names
the wrong implementation in its docstring. The same applies to fusion: an RRF
test over two candidate lists that *agree* proves nothing about fusion.
**The RRF tiebreak trio is three survivors and one property, verified rather
than trusted.** Removing `top.id` from either lane's `row_number()` window, or
`t.id` from the lexical lane's inner `ORDER BY`, each survives the whole suite
— the window reads an input the inner `ORDER BY` has already totally ordered.
Removing **both from the lexical lane at once** kills
`test_tied_scores_are_broken_deterministically_and_survive_a_rewrite`, exactly
as `adapters/search/postgres.py`'s own comment claims.

**The embedder is optional and off by default.** `USHER_EMBEDDING_ENABLED`
gates it and `worker.register(JobKind.INDEX, …)` is guarded on the embedder
being present exactly as `ENRICH` is guarded on `provider is not None`, so a
worker never claims work it cannot run. Without it, full-text and trigram
still serve all 1.27M titles — narrowed, not broken — and `--mode semantic`
refuses outright while `--mode fused` narrows to full-text *and says which*.
`usher index` loads no model at all: staleness is a question about a recorded
model **name**.

**Query expansion was measured on 2026-08-07 and it makes retrieval worse, so
it ships behind its own switch, default off.** PRD 05 has called it the
cheaper, better-evidenced lever for mood queries since M1 — on the literature's
authority, with nothing in this project measuring it. Run against the local
vLLM serving `gemma-4-26b-a4b`, over **5 mood queries × 150 real TMDb overviews
for the 150 most-voted catalog titles**, embedded with the shipped
`compose_document` and the shipped `FastEmbedEmbedder`, **targets written down
before any cosine was computed**:

| | raw query | expanded |
|---|---|---|
| MRR | **0.733** | 0.373 |
| recall@10 | **0.800** | 0.533 |

The typed query wins **4 of 5** queries outright and ties the 5th. **The
label-free control is what makes this a mechanism rather than a bad draw**:
pairwise cosine *between the five queries themselves* rises **0.5417 → 0.5975
mean and 0.6328 → 0.7784 max** after rewriting — five distinct searches come
back more alike than they went in — and the top hit's z-score falls in 3 of 5.
The rewrites are generic critic prose (*"A dramatic exploration of profound
isolation and psychological survival…"*) that collapses toward the corpus
centroid, and *Arrival*, *Seven*, *Requiem for a Dream* and *Prisoners*
dominate the expanded top-5 of **unrelated** queries.

**The caveat travels with the numbers and is not small:** one model, one
150-document corpus, five queries. It is thin — and it is the only measurement
that exists, against a PRD claim that rested on the literature alone, so the
default follows it. `USHER_QUERY_EXPANSION_ENABLED` is therefore a **second**
setting, independent of `USHER_LLM_ENABLED` and `false` even when that is
`true`; the fourth combination (expansion on, no client) is refused by a
`Settings` validator rather than silently ignored, because a knob that is on
and means nothing is the failure `extra="forbid"` exists to prevent. M8 Task
20's argument that *"a second setting's only honest default is 'follow the
first'"* was sound while expansion was believed to help and is superseded by
this measurement, not deleted.

**And expansion is billed on searches the semantic lane cannot serve, which
this run also found and which is not fixed.** The guard is `embedder is None`,
not *"anything is embedded"*. Measured with `USHER_EMBEDDING_ENABLED=true` and
`title_embeddings` empty: `usher search --mode fused` bought a completion,
printed `expanded: …`, returned `semantic_coverage=0.000`, and *then* printed
*"no title in the filtered population has an embedding"* — **the warning
arrives after the money**, on every fused search of every not-yet-backfilled
deployment. `--mode full_text` correctly bought nothing. Not repaired here
because the correct predicate — *does any title in the **filtered** population
have a vector* — is not answerable before the vector that does the filtering
exists, and the cheap global stand-in (`SELECT 1 FROM title_embeddings LIMIT
1`) is a different guard costing a new `TitleEmbeddingRepository` port method,
two implementations, a contract case and a read on every fused search. The new
setting reduces the exposure to deployments that opted in; it does not close
it.

**Its position is the whole cost argument.**
`QueryExpansionService.expand` is called from exactly one
line -- the line before `SearchService`'s `self._embedder.embed([...])`, inside
the `else` of the `embedder is None` branch. Four things follow and each is a
case: a `full_text` search buys no completion (no embed to sit in front of), a
deployment with no embedder buys none (`semantic` raises and `fused` narrows
before reaching it), a blank query buys none (refused before the model), and
**`usher suggest` buys none** -- `SuggestIndex` is its own port with no
semantic lane, which is what keeps a completion off the path a client drives
per keystroke. The unit of spend is one search that was going to embed
something.

Three decisions worth not re-deriving. **Only the vector comes from the
rewrite**: `SearchRequest.query` stays the typed string, so under RRF the
lexical lane still matches the viewer's own words and a rewrite that drifted
cannot take an exact-title search with it. **`SearchService.expander` is
`QueryExpansionService | None`-shaped optional and
`QueryExpansionService.client` is not** -- M8's rule everywhere else is that an
`LLMClient` holder is built or not built (`CurationService`), and that works
only because a deployment with no LLM runs no curation; a `SearchService` is
built on every deployment there is, so "built or not built" has no state left
to express and the choice is between an optional collaborator and a second
class. It is the shape this project has never needed for `embedder`, one
parameter over — a precedent by absence: ADR-0022 argues the embedder is
optional and never considers or refuses a second `SearchService` class, so
"the same call ADR-0022 already made" (which this file and `services/search.py`
both said until 2026-08-07) overstated it. **A failure is absorbed**:
`expand` never raises, and an unreachable endpoint, an unparseable answer or a
blank/over-long rewrite all leave the search running on the typed query --
while still writing the `llm_calls` row, because a ledger holding only the
successes understates spend by exactly the failures.

**And the reported-not-substituted rule has a shape, and it is one-directional
— this file claimed a biconditional until 2026-08-07 and the biconditional is
false.** `SearchAnswer.expanded_query` is the text that was embedded and `None`
when the query was embedded as typed, so **a populated field means a completion
was bought, and an absent one means nothing about spend**. `usher search`
prints it above the results. The counterexample is measured, not hypothetical:
a completion that answers with the wrong key is bought and billed in full —
`tokens_in=1000`, `tokens_out=200`, one `llm_calls` row, `ok=False`,
`error=NO_USABLE_QUERY` — and `expand` returns `None`, so the field is `None`
and the CLI prints nothing. Same for an upstream failure. The old wording
(*"populated on exactly the searches that bought a completion"*) invites *"no
`expanded:` line ⇒ no spend"*, which is exactly backwards for the two failure
paths the billed-on-every-attempt rule exists to make visible. A field echoing
the typed query when nothing was expanded would instead put a line on every
search of every default deployment and mean nothing -- which is the mutation
the CLI case kills.

The `usher similar --rebuild` freshness gap itself is stated in `CLAUDE.md`
(always loaded); the one detail not there: `title_neighbors` carries a
whole-artefact `computed_at` rather than a per-pair fingerprint, which is
exactly why no per-row predicate can detect the staleness.

## The blend's weight ceiling is open, and the boundary value inverts by one ulp (2026-08-11, M9 F5)

**Measured while adding PRD 05's sixth ranking term, against the bound M6 and
M9 F4 had both stated as an inequality with a slack of 0.01.**

`_blend` renormalises over present signals, so the displacement bound compares
*numerators*: a rank-0 hit with every other signal against it scores `0.70`,
and a rank-1 hit with every other signal maximally for it scores
`0.35 + 0.15 + 0.15 + 0.02 + 0.02 + w`. F4's constant comment read *"0.34
against a ceiling of 0.35 leaves 0.01"*, which invites taking 0.01.

| taste weight `w` | challenger numerator | exact match at 0.7 | verdict |
|---|---|---|---|
| 0.01 | **0.7000000000000001** | 0.7 | **exact match DISPLACED** |
| 0.009 | 0.6999000000000001 | 0.7 | holds, margin 1.0e-04 |
| 0.005 | 0.6950000000000001 | 0.7 | holds, margin 5.0e-03 |

At `w = 0.01` the challenger is **one ulp above** — `0.7` is
`0.69999999999999995559…` and the left-to-right sum overshoots it — so the
sort key `(-score, title_id)` puts the challenger **first regardless of id**.
It is an inversion, not a tie, and `_blend`'s summation order is the call
site's kwargs order, so it is not even stable against reordering the argument
list. **A bound stated as "sums below half" has to be read as strict, and the
boundary value has to be evaluated in floating point rather than on paper** —
same family as `mutation-sweeps.md`'s *"before pinning an exact number computed
through floating point, check the arithmetic in the interpreter"*, arriving at
a design constant rather than at a fixture.

Two consequences carried in code. The shipped weight is **0.005**, the midpoint
of the open interval `(0, 0.01)` — 0 excluded because a zero-weighted term is a
weight that reads like a signal, 0.01 excluded by the table above, and nothing
measured distinguishes any point between (`title_embeddings` holds **0 rows**
on both surviving catalogs, so the term's effect size is unmeasurable today,
not merely unmeasured). And the bound is now pinned by a case that calls
`_blend` **directly** rather than through a fixture: *"popularity maximally for
it"* is asymptotic — `p / (p + 10)` never reaches 1.0 — so no seeded catalog
can reach the corner the bound is about, and an ordering case at any reachable
configuration is green under every `w` in the table.

**What a 0.005 term can move, so the weight is not mistaken for a measurement
of the signal.** With all six present the denominator is 1.045, so the term
spans 0.0048 of score. It cannot overturn `owned` (0.15) or `played` (0.02) at
any cosine gap. It overturns one relevance step only where
`0.005·Δcos > 0.70/((1+k)(2+k))`, i.e. **k ≥ 11** at an impossible `Δcos = 1.0`
and k ≥ 25 at a realistic 0.2. Where it decides is where the other five have
tied — which `_dense_ranks` makes ordinary rather than rare, because equal
index scores share a rank and the relevance term then cancels exactly.
