# ADR-0002 — Postgres-first search, Meilisearch behind a measurable gate

**Status:** Accepted for the full-text and semantic halves, which the gate does
not test and which the same run measured favourably. **The gate itself was run
against the real catalog on 2026-08-03 and it FAILED** — not marginally, and on
both halves of a bar stated before the numbers were known. Reverses an earlier
recommendation; implemented in M6. **M6 adds no Meilisearch either way**
(PRD 09's boundary call 7); the deliverable is the recorded failure, this ADR
amended, and a scoped follow-up with an owner. See "Evidence — the gate,
measured".

⚠️ **Amended again 2026-08-19 by
[ADR-0040](0040-rating-columns-name-their-source.md), in two places, both marked
inline below.** The gate's sampling frame no longer reproduces on
`vote_count` — that column acquired a second writer — and is re-anchored on
`imdb_num_votes`, where it reproduces to **+0.19%**. And the vote-count tiebreak
this ADR added was resting on that same dual write, so it now reaches nothing on
a bootstrap-only catalog. Neither is a reversal of this decision; both are facts
it rested on that stopped being true.

**✅ That follow-up is discharged, and its outcome is
[ADR-0031](0031-the-two-tier-suggest.md) — the two-tier suggest.** M9 built
tier 1 (`PostgresPrefixSuggestIndex`), measured it at catalog scale against a
bar committed beforehand, and put both tiers on one route as
`GET /search/suggest?tier=prefix|fuzzy`. **Read ADR-0031 before re-opening
anything in this document about the type-ahead box**, and read it *particularly*
before quoting consequence 2's *"the only thing measured that fits inside a
keystroke"*, which that ADR narrows: the figure is real over whole names and a
keystroke is not a whole name — at a one-character prefix the same statement is
291 ms on `titles` alone and 2,707 ms over the union, so tier 1 is a keystroke
path from **seven characters up** and the route declines to run it below four.
Meilisearch remains gated exactly as this document scopes it and is still not
built; nothing here is reversed.

## Context

The initial design specified PostgreSQL as the canonical store plus Meilisearch
as a dedicated search and vector index. Research to validate that split
undermined its own premise.

## Decision

v1 implements search entirely in PostgreSQL: weighted `tsvector` full-text, a
separate `pg_trgm` + `fuzzystrmatch` autocomplete path, and `pgvector` for
embeddings, fused with Reciprocal Rank Fusion.

`SearchIndex` is an ABC. Meilisearch may be added later for the instant-search
box only, gated on a **measurable** failure: recall@5 on a typo test set built
from real catalog titles weighted toward short names — **and, from 2026-08-02,
latency on the same set.** Recall alone was the original wording and it is
insufficient: a configuration can clear a recall bar while being multiples too
slow for the box the gate exists to serve, which is precisely what happened.
The gate is both numbers, per typo class and per length band, with sample
sizes.

## Consequences

**Gained:** no dual-write synchronisation, no ghost documents, no
reindex-on-facet-change, no second stateful service, transactional consistency
between catalog and index, and arbitrary SQL for ranking blends.

**Given up:** typo tolerance is genuinely weaker. This is the real cost and it
is not hand-waved — see below.

**Retained:** the upgrade path costs one ABC implementation because nothing
above the port knows which engine answers.

**Paid, and now quantified.** The instant-search box specifically did not
clear the bar. Over 2,993 single-edit typo cases on 750 real catalog movie
names, the shipped path finds the right title **27.8% of the time for a
2–4-character name** and **68.3% for a 5–7-character one**, and the best
configuration measured — under any threshold, any cap, either index type —
reaches only **47.9%** on that shortest band. No configuration comes within
**6×** of an as-you-type latency budget. The decision was always conditional;
its condition has now fired, and three things follow:

1. **`pg_trgm` + `levenshtein_less_equal` is a good *fuzzy* path and not a
   *keystroke* path.** From 8 characters up it is 95–100% at every typo class,
   which is most of a real catalog. It is the short one-word name — `Up`,
   `Her`, `Dune` — where it does not work, exactly where this ADR said it
   would not.
2. **A two-tier suggest is what the numbers support, and it is scoped in
   PRD 09 with an owner (M9).** A btree `lower(name) text_pattern_ops` prefix
   probe answers the same 2,993 queries at **p50 0.6 ms / p95 1.0 ms /
   max 10 ms** — 200–330× faster than any fuzzy configuration and the only
   thing measured that fits inside a keystroke. It has no typo tolerance at
   all (1.9%), so the two are complements rather than alternatives: the btree
   on every keystroke, the trigram path debounced behind it.

   **Amended 2026-08-12 by M9's B3, which measured the shipped tier-1
   statement rather than the probe, and the amendment is a narrowing of the
   claim rather than a reversal.** Those figures reproduce — p50 0.664 / p95
   0.947 ms over the same 2,993 strings on the same catalog, and p95 1.465 ms
   once the statement unions `title_search_names` at 10,896,525 rows. But
   *"the only thing measured that fits inside a keystroke"* was measured on
   whole mutated names, which are long and selective, and **a keystroke is
   not that**: at a one-character prefix the same statement is **291 ms on
   `titles` alone and 2,707 ms over the union**, missing a 10 ms budget on
   both arms at every prefix shorter than seven characters. Tier 1 is a
   keystroke path from seven characters up. The mechanism is the `UNION`'s
   de-duplication spilling to disk and a lossy bitmap heap recheck, **not**
   the sort, which is a 26 kB top-N heapsort. Full curve, both arms, in
   `.claude/rules/search-and-embeddings.md`; the route-level consequence
   belongs to ADR-0031.

   **Separately verified, because this ADR's own evidence says an added index
   can silently tax the shipped path:** `m09a`'s two btrees do **not**. The
   identical 2,993 cases, within one run, with both prefix indexes present and
   then both dropped: **39.593 ms against 39.571 ms p50, ratio 1.001,
   byte-identical recall**. The GIN/GiST result does not generalise to a
   btree, and it needed measuring rather than reasoning about.
3. **Meilisearch remains a post-v1 candidate and is now a *justified* one
   rather than a hypothetical.** [ADR-0021](0021-the-suggest-path-is-its-own-port.md)'s
   port split is what keeps that follow-up at one class plus a write path,
   and the write path is the dual write this ADR refused — so adding it stays
   a deliberate, visible act.

## Evidence

**The headline argument against Postgres does not apply to this workload.**
The widely-cited benchmark showing 8–25 s `ts_rank` queries is driven by
match-set cardinality — it appears when a single query matches ~1M rows, which
long-text search produces and title search does not. Two details are almost
always omitted from citations of it: the same article's own mitigation (capping
candidates at ~10k) brought it to 144–400 ms, and the authors *started* with a
34k movie dataset and abandoned it because it was too small to show any
difference against Elasticsearch. A competing vendor concedes the inverse:
for terms matching 1–10 documents, Postgres full-text search is faster than
theirs.

**Ranking blend is application code regardless of engine.** Meilisearch's custom
ranking rules are only `attribute:asc|desc` applied as bucket-sort tiebreakers —
no arithmetic exists in the API. Typesense caps `sort_by` at three fields and
has an open, explicitly non-committed feature request for expression scoring.
Engines that *can* evaluate `0.6·semantic + 0.2·log(popularity) + 0.2·recency`
in-engine (Vespa, Elasticsearch `script_score`) are not appropriate for a
single-host solo-maintained deployment. So the engine is a candidate generator
and the blend is ours either way — which is precisely what makes starting
simple low-risk rather than a gamble.

**The genuine weakness is typo tolerance.** `pg_trgm` is trigram-set overlap,
not edit distance. A four-character word yields ~5 trigrams, so one typo
destroys most of them, and transpositions are close to a blind spot — `Up`,
`Her`, `Dune`, `Alien` are the worst case. A Postgres vendor's own comparison
concluded that only Typesense and Meilisearch handled misspellings properly.
Mitigation is the narrow autocomplete path with `levenshtein_less_equal` re-rank
over a capped candidate set; `fuzzystrmatch` is a core contrib module, so
Postgres does have edit distance — it simply isn't indexable and must run over
a narrowed set.

**Typesense is excluded outright.** It is fully memory-resident with no on-disk
index; the index rebuilds into RAM on every start and the server returns HTTP
503 throughout — officially "5–60 minutes depending on dataset size", roughly
2–15 minutes at this scale. The maintainers rejected dump-on-shutdown and
recommend a 3-node HA cluster instead. Unacceptable on a host that reboots for
kernel and GPU driver updates.

**Scale is not the constraint.** At 1M × 384 dimensions on a 64 GB host, every
option's memory footprint is noise (pgvector ~2–3 GB HNSW, ~1.5 GB with
`halfvec`). *(384 was the shipped width when this was written;
[ADR-0038](0038-the-embedding-width-is-deployment-wide-ddl.md) moved it to
1024, so the `halfvec` figure is ~4 GB — which is still noise on a 64 GB host,
so the claim this parenthetical supports is unaffected. Recorded rather than
rewritten: the number is what the decision was taken on.)* Published RAM objections are calibrated for per-GB cloud billing or
20M+ document corpora.

## Notes for implementation

Each of these was implemented in M6, and three of them turned out to need a
correction that is recorded where it belongs rather than here.

- pgvector ≥ 0.8.5 — CVE-2026-3172 and HNSW vacuum corruption fixes. The
  image ships **0.8.6**, so the floor is met.
- `hnsw.iterative_scan = relaxed_order` is **off by default** and must be set,
  or filtered vector search suffers severe recall collapse. **Measured in M6
  and it is worse than "recall collapse" suggests**: at 2% filter selectivity
  the default returns **0.88 rows on average against a request for ten** —
  HNSW visits `ef_search` candidates, the filter kills them, and the scan is
  already over. That is an empty endpoint, not a degraded ranking, and
  `ef_search` is the wrong lever **for that failure** (40 → 200 with the GUC
  off still yields 4.24 of 10). `relaxed_order` beats `strict_order` on recall,
  0.508 against 0.100, because strict ordering terminates earlier to buy a
  guarantee RRF re-ranking does not need. ⚠️ **"`ef_search` is the wrong lever"
  was written without that qualifier and read as a fact about the shipped
  configuration, which it is not** — the sentence is about `iterative_scan =
  off` at 2% selectivity on uniform-random 384-lane vectors. With the GUC on,
  on 132,409 real 1024-lane vectors, unfiltered, `ef_search` is precisely the
  lever: recall@10 against an exact scan is 0.858 at 100 and 0.917 at 200 over
  12 typed plot queries, monotone at every one of them, and
  `Settings.search_hnsw_ef_search` moved to 200 on 2026-08-19 (issue #32). The
  decision this ADR records — `relaxed_order`, set with `SET LOCAL` — is
  unchanged and was not in question. ⏳ **A cheaper answer exists and is not built**:
  at high filter selectivity a plain btree on the filter column lets the
  planner abandon HNSW and answer exactly, which would make the ANN question
  exist only inside a selectivity band. Recorded here rather than shipped —
  no such index is in M6, and choosing the band deliberately needs a run
  against real embeddings.
- GIN `fastupdate = off`; the default pending-list buffering causes p99
  spikes. **The mechanism is now measured**: a 1.6 MB pending list cost 231
  buffers against 30 — 7.7× read amplification on the index stage, invisible
  in `EXPLAIN` unless you look at buffers.
- Fuse with RRF, never by adding scores from incompatible scales. **Five
  traps were reproduced in M6**, one of which is silent and total:
  `row_number()` returns `bigint`, so `1 / (60 + rank)` is integer division —
  every score becomes `0.0`, the result comes back in `id` order, and nothing
  errors.
- **The `pg_trgm` mitigation named above — a capped candidate set plus a
  `levenshtein_less_equal` re-rank — is mandatory rather than advisory.**
  This ADR's cardinality argument holds and is sharper than it was stated: at
  a constant 1.32M corpus, latency spans 0.12 ms → 556.76 ms driven entirely
  by match-set size, and ranking has **no `LIMIT` pushdown** — of a 601.9 ms
  ranked query, 42 ms is the index scan and the other 560 ms is fetching
  650,000 heap tuples so `ts_rank_cd` can score them. **The gate below
  qualifies this in one direction:** the cap is mandatory for *latency* and
  is not the *recall* lever it was written up as — on 1,271,138 real names it
  truncated 0.0% of the shipped configuration's misses, and the re-rank
  dropped 0.0% of them in every configuration measured.

## Evidence — the gate, measured

**Run 2026-08-03 against a real 1,271,138-title catalog** (`pgvector/pgvector:pg17`,
PostgreSQL 17.10, pgvector 0.8.6, `pg_trgm` 1.6, `fuzzystrmatch` 1.2), driving
the shipped `PostgresSuggestIndex` — the real ordered cap, the real
`levenshtein_less_equal` re-rank, one fresh transaction per query — from a
throwaway script outside the working tree. **The test set is built from real
catalog titles and is therefore not committed**; what is committed is the
measurement and the procedure that regenerates it.

### The bar, written down before the numbers were known

- recall@5 **≥ 0.90** on the 8–11, 12–19 and 20+ bands; **≥ 0.85** on 5–7
  (interpolated, because the plan named only "the 8+ bands" and "the 2–4
  band" and a band with no bar cannot fail); **≥ 0.75** on 2–4; **no single
  typo class below 0.60 in any band**.
- **p95 ≤ 50 ms**, end to end. p95 and not p50: a type-ahead box that stutters
  on one keystroke in twenty is a box that stutters, and p50 is the statistic
  that lets a bimodal path look fine.
- Both halves, for one single configuration, or the gate is not closed.

### How the set was built, in enough detail to regenerate it

Movies only, `vote_count ≥ 500` (a floor high enough that the name is one a
person would plausibly type; the 1.27M-row IMDb skeleton is mostly obscure
television and shorts). Names not unique in the catalog were excluded at
sampling time — **81,054 lower-cased names are shared by more than one
title**, and recall@5 for a name three titles share is a question about
disambiguation rather than about typo tolerance. Five bands over
`char_length(name)` with an **equal draw from each**, which is the "weighted
toward short names" instruction made concrete and deliberately *not* the
catalog's own distribution (39.4% of it is 12–19 characters and 34.4% is
20+, against 1.6% at 2–4). Eligible pool and draw per band:

| band | eligible after the floor and the uniqueness filter | drawn |
|---|---|---|
| 2–4 | 432 | 150 |
| 5–7 | 2,532 | 150 |
| 8–11 | 7,178 | 150 |
| 12–19 | 20,520 | 150 |
| 20+ | 17,887 | 150 |

⚠️ **Amended 2026-08-19: the frame is now `imdb_num_votes >= 500`, and the
threshold is the only part of this paragraph that did not have to move.**
`titles.vote_count` acquired a second writer — TMDb enrichment, counting a
different electorate — so by 2026-08-19 this predicate selected **8,523**
unique-named movies rather than 48,549, and `usher eval suggest --full` refused
to record a baseline against it. The column was split by source
([ADR-0040](0040-rating-columns-name-their-source.md)) and the frame re-anchored
on the IMDb half, which no TMDb crawl can move. **It reproduces to +0.19%** —
428 / 2,541 / 7,097 / 20,425 / 18,146 = **48,639**, `shared_lower_names`
**81,088** against 81,054, and **2,991** cases rather than 2,993 (the 2–4 band
now draws nine names admitting no deletion where it drew seven). The residual is
an eight-day-newer IMDb dump moving titles across the threshold in both
directions, not a different frame — so a run comparing new numbers with the ones
below carries that caveat, alongside the one this section already carries about
the 750 drawn names never having been recorded.

Four typo classes per name — substitution, deletion, transposition, doubled
letter — one mutation each at a uniformly random position, `random.Random`
**seed 20260803**. 750 names × 4 = **2,993 cases** (seven two-character names
admit no deletion). A hit is the title the typo was generated *from*
appearing in the five returned; the identity is known because the script
generated the mutation.

### The result: the shipped path, per typo class and per length band

`PostgresSuggestIndex` as M6 shipped it — GIN `gin_trgm_ops`, `%` at the
`Settings` default floor of 0.3, ordered cap 200, `levenshtein_less_equal ≤ 2`:

| name length | substitution | deletion | transposition | doubled letter | all | n per cell |
|---|---|---|---|---|---|---|
| 2–4 | 19.3% | 12.5% | **0.0%** | 78.7% | **27.8%** | 144–150 |
| 5–7 | 90.7% | 48.0% | 35.3% | 99.3% | **68.3%** | 150 |
| 8–11 | 99.3% | 88.7% | 94.7% | 99.3% | **95.5%** | 150 |
| 12–19 | 100.0% | 99.3% | 100.0% | 100.0% | **99.8%** | 150 |
| 20+ | 99.3% | 98.7% | 100.0% | 100.0% | **99.5%** | 150 |
| **all** | **81.7%** | **69.9%** | **66.1%** | **95.5%** | **78.3%** | 2,993 |

p50 **33.3 ms**, p95 **208.8 ms**, max **734 ms**. When the true title was
returned it was usually first — median rank 1, and 2,062 of 2,344 hits at
rank 1.

**Verdict: fails.** 2–4 at 27.8% against a bar of 0.75, 5–7 at 68.3% against
0.85, five class/band cells below 0.60, and p95 4× the budget. **Transposition
on a 2–4-character name is 0.0% — not "close to a blind spot" but a total
one**, which is this ADR's own sentence arriving as an exact number.

### After honest tuning: every configuration measured

"After honest tuning" means both knobs were moved and the number recorded at
each setting, not that one configuration was tried. Same 2,993 cases
throughout.

| configuration | recall@5 | 2–4 band | transposition | p50 | p95 | max |
|---|---|---|---|---|---|---|
| GIN `%` @0.3, cap 200 — **as M6 shipped** | 78.3% | 27.8% | 66.1% | **33.3 ms** | 209 ms | 734 ms |
| GIN `%` @0.2, cap 200 | 78.3% | 32.7% | 67.7% | 128.7 ms | 704 ms | 989 ms |
| GIN `%` @0.1, cap 200 | 77.6% | 30.2% | 67.2% | 470.1 ms | 928 ms | 1,475 ms |
| GIN `%` @0.3, cap 200, **+ vote-count tiebreak** — as it ships now | **82.5%** | 36.1% | 69.2% | **33.6 ms** | 211 ms | 730 ms |
| GIN `%` @0.1, cap 200, + vote-count tiebreak | 85.1% | 46.9% | 74.6% | 469.2 ms | 926 ms | 1,487 ms |
| GIN `<%` (`word_similarity`) @0.3, + tiebreak | 78.1% | 30.0% | 64.8% | 46.1 ms | 263 ms | 631 ms |
| GiST KNN `ORDER BY name <-> q`, cap 200 | 77.7% | 30.2% | 67.2% | 198.5 ms | 304 ms | 428 ms |
| **GiST KNN, cap 200, + vote-count tiebreak** | **85.3%** | **47.9%** | **74.8%** | 198.1 ms | **304 ms** | **428 ms** |
| GiST KNN, cap 1000, + vote-count tiebreak | 83.4% | 43.8% | 72.9% | 201.9 ms | 311 ms | 440 ms |
| btree `lower(name) text_pattern_ops` prefix | 1.9% | 1.9% | 0.1% | **0.6 ms** | **1.0 ms** | **10 ms** |

**Nothing passes.** The best recall available anywhere in this table is 85.3%
overall and **47.9% on the band the gate exists to interrogate**, at a p95 six
times the budget. Lowering the trigram floor buys nothing on this catalog and
costs 4–14× latency; raising the cap makes recall *worse*.

### Where the misses go, which a single recall number cannot say

For a random 250 of each configuration's misses, the true title was traced
back to the stage that lost it:

| configuration | below the `%` floor | truncated by the cap | dropped by the re-rank | out-ranked in the final ORDER BY |
|---|---|---|---|---|
| GIN `%` @0.3 (no tiebreak) | **63.6%** | 0.0% | **0.0%** | 36.4% |
| GIN `%` @0.2 | 26.0% | 7.6% | 0.0% | 66.4% |
| GIN `%` @0.1 | 4.0% | 24.8% | 0.0% | 71.2% |
| **GIN `%` @0.3 + tiebreak — the configuration that ships** | 82.8% | 0.0% | 0.0% | 17.2% |

**The first row said "as shipped" and the last row is what ships**, which is a
label this document carried until 2026-08-12 and which cost B3 a
reconciliation: M9's plan quoted the last row's p50 (33.6 ms) beside the first
row's shares (63.6/36.4), and no single run of `_SUGGEST` can satisfy both.
Re-measured on the same catalog with a different draw of 750 names, the shipped
row is **90.8 / 0.0 / 0.0 / 9.2** — the floor/out-ranked balance moves with the
sample, and **the two zeros are exact in every row of this table and in the
re-run**, which is the part the claim rests on.

Three things follow, and two of them contradict what this project assumed.

- **The candidate cap is never the binding constraint at the shipped
  configuration, and the `levenshtein` re-rank never drops the true title —
  0.0%, in every configuration measured.** M6's whole design story put the
  cap at the centre; on real data it is inert until the floor is dropped, at
  which point the cap becomes a *new* defect rather than the cure.
- **Lowering the floor does not convert misses into hits. It converts
  threshold-excluded misses into out-ranked ones**, which is why recall is
  flat-to-worse from 0.3 to 0.1 while latency grows 14×. The synthetic dry
  run's 66.2% → 93.5% does not reproduce at 1.27M names with real
  competitors.
- **The final `ORDER BY` was the other half of the loss, because
  `titles.popularity` is NULL on all 1,271,138 rows.** Nothing in `src/`
  writes that column except TMDb enrichment, and boundary call 4's own
  premise is that the enriched tier is 2k–10k titles. So
  `ORDER BY dist ASC, popularity DESC NULLS LAST, id ASC` degenerated to
  `dist ASC, id ASC` — equal-distance candidates ordered by a UUIDv7, i.e.
  by insertion order. **Adding `vote_count DESC NULLS LAST` under popularity
  — a column the bootstrap itself fills, 538,937 rows — is worth +4.2 points
  overall and +8.3 on the 2–4 band at unchanged latency, and it shipped with
  this run**, pinned by
  `tests/integration/test_adapters_search_postgres.py::test_vote_count_orders_the_box_when_every_popularity_is_null`.

  🔴 **⚠️ Amended 2026-08-19: *"a column the bootstrap itself fills"* is no
  longer true, so this +4.2/+8.3 is now the size of what a fresh catalog has
  lost.** The bootstrap filled it because the IMDb bulk loader wrote IMDb's
  `numVotes` into the same column TMDb enrichment wrote — **this tiebreak was
  resting on the dual write that
  [ADR-0040](0040-rating-columns-name-their-source.md) exists to end.** The
  loader now writes `imdb_num_votes`; nothing but enrichment reaches
  `tmdb_vote_count`, and on a bootstrap-only catalog it is NULL on every row, so
  wherever popularity is absent too — all of the `--phase imdb` catalog this
  gate ran against, ~77% of a `--phase all` one — the `ORDER BY` falls back to
  exactly the `dist ASC, id ASC` this bullet measured as the defect. Both keys
  are `NULLS LAST`, so the regression is silent. **Deliberately not repaired**: re-pointing the key at `imdb_num_votes`
  is a ranking change owing its own measurement, and it is
  [#39](https://github.com/anirudhlath/usher/issues/39).

  **Re-measured 2026-08-05 (M7 Task 36), and both the finding and its scope
  are now sharper.** "NULL on all 1,271,138 rows" was true of the gate's
  **`--phase imdb`** catalog; `link_crosswalk` writes `popularity` from
  `tmdb_ids`, so a `--phase all` catalog carries it on **291,584 of 1,271,570
  titles (22.9%), of which exactly 3 are 0.0** — real values, not filler. The
  +4.2/+8.3 win above was therefore measured where popularity was inert. On
  the populated catalog popularity re-contests the ordering and costs **1.3
  points overall (83.4 → 82.1)**, all out-ranked misses — within Task 36's
  2.0-point regression bar, so the ordering was **kept unchanged** and the
  "partially populated catalog is worse than either extreme" worry is refuted.
  Dropping popularity (vote_count as the primary key) recovers all 1.3 points
  and does not hurt the all-NULL arm, but its enriched-tier behaviour was
  unmeasurable on a skeleton catalog and is deferred to M9; `NULLIF(popularity,
  0)` recovers nothing, since only 3 zeros exist.

### GIN against GiST, which this ADR left ⏳ and which is now decided

**Neither wins outright, they trade, and the two must not both exist.**

| | GIN `gin_trgm_ops` | GiST `gist_trgm_ops` |
|---|---|---|
| build over 1,271,138 names | **5.394 s** | 11.800 s |
| index size | **75 MB** | 139 MB |
| best recall@5 measured | 82.5% | **85.3%** |
| best 2–4-band recall | 36.1% | **47.9%** |
| p50 | **33.6 ms** | 198.1 ms |
| p95 / max | 211 ms / 730 ms | **304 ms / 428 ms** |

GIN is 6× faster at p50 and half the size; GiST buys 2.8 points of recall,
11.8 on the short band, and a much tighter tail (its max is 428 ms against
GIN's 734 ms, because KNN traversal cost is nearly independent of match-set
size while `%` is not). **GIN stays**, because p50 is what a keystroke pays
and no amount of recall rescues a path that is 6× over budget anyway.

**And the two cannot simply coexist, which is the trap worth recording.**
With a GiST trigram index present *alongside* the GIN one, the planner takes
GiST for the `%` operator: the identical shipped configuration went from p50
33.3 ms to **141.5 ms** (4.3×) with byte-identical recall (78.3% both ways).
So "add GiST for the KNN path and keep GIN for `%`" is not available — adding
the second index silently taxes the first.

### The full-text half is unaffected, checked rather than assumed

One `websearch_to_tsquery` sample through the shipped `_FULL_TEXT` statement
at 1,271,138 titles, 15 queries × 5 runs: **0.5 ms to 20.2 ms**, driven
entirely by match-set size (15 matches → 0.64 ms; 17,616 matches → 20.15 ms).
That is this ADR's cardinality argument holding on the workload it was made
for: a *title* corpus never produces the 650,000-row match sets that make
`ts_rank_cd` expensive, and the whole full-text path is comfortably inside
the budget the type-ahead path misses.

## Uncertainty

**Narrowed, not filled.** This run measured `pg_trgm` +
`levenshtein_less_equal` against *our own catalog*, which is what the gate
asked for. It is still **not** the head-to-head against Meilisearch or
Typesense on a labelled dataset — no rigorous public benchmark of that
appears to exist, and this run did not build one. The hole this ADR named
still sits under the *comparison*; what it no longer sits under is the
absolute number.

Named rather than implied, this run could not settle:

- **Real typed queries.** Every case here is a synthetically mutated real
  title. People also truncate, abbreviate, reorder words, and type the
  article they think a film has. The measurement that replaces all of this
  is [10](../10-telemetry-and-dashboards.md)'s `search_queries` table —
  zero-result and no-click rates on queries somebody actually typed —
  assigned to M9 because three of its seven columns need an HTTP surface to
  fill.
- **Multi-typo queries.** One mutation per case, and `_MAX_DISTANCE = 2` is
  the shipped ceiling, so a two-typo query is out of reach by construction.
- **Non-Latin scripts.** `pg_trgm` extracts trigrams over characters and pads
  on word boundaries; a name in a script with no spaces behaves differently
  and no case here tests one.
- **The head-to-head**, deliberately: boundary call 7.
- **Whether an enriched catalog changes the answer.** Every measurement here
  is on a bootstrap-only catalog with `popularity` NULL throughout, which is
  the honest state of a fresh deployment and *not* the state of one that has
  been enriched for a month.
