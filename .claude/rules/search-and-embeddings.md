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
  back to the stage that lost them: at `GIN % @0.3 cap 200` **63.6% fell
  below the `%` floor, 36.4% were out-ranked, 0.0% were truncated by the cap,
  0.0% were dropped by the re-rank** — and the re-rank figure is 0.0% in
  *every* configuration measured. M6's design story put the cap at the
  centre; on real data it is inert until the floor is dropped, at which point
  it becomes a new defect (24.8% at 0.1) rather than the cure.
  **"At the shipped configuration" was the wrong label on those two numbers
  and is corrected here — 63.6/36.4 is the row *without* the vote tiebreak,
  and the tiebreak shipped in the same run.** The configuration that ships is
  `GIN % @0.3 cap 200 + vote tiebreak`, whose split is **82.8 / 0.0 / 0.0 /
  17.2**, one table row below. M9's B3 was sent to reproduce "the shipped
  split" and found the plan quoting this row's shares beside the other row's
  p50 — a pairing no single run of `_SUGGEST` can satisfy. The two zeros are
  the half that carries the claim and they are identical in both rows.
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
  **Read "in `SuggestIndexContract`" as the class hierarchy since M9's B2, not
  as one class.** That contract split in two when the prefix tier arrived: the
  typo cases moved down to `TypoTolerantSuggestIndexContract`, which subclasses
  it, so the sentence above is still true in the is-a sense and no longer names
  where to look. The three typo cases are on the subclass, and the two
  implementations that sign it are `PostgresSuggestIndex` and `FakeSuggestIndex`;
  `PostgresPrefixSuggestIndex` signs the base only, on purpose. **This divergence
  is therefore narrower than it was and the narrowing is not an improvement to
  it** — tier 1 has no trigram floor to be wrong about, and the whole of the
  0.1-versus-0.3 gap still sits on the tier that is now debounced behind it.
  Leaving the typo cases on the base and skipping them for tier 1 was considered
  and refused for a reason this file should carry: **a skipped case reads as
  coverage in the summary line and asserts nothing**, and a tier whose entire
  design is the *absence* of typo tolerance would then be described by three
  permanent skips rather than by one integration case that asserts the absence
  and proves the path ran first.
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

**Tier 1 measured at catalog scale on 2026-08-12 (M9's B3), against a bar
committed before the run, and the headline is that it passes the bar it was
given and fails the job it exists for.** Same host, same 1,271,138-title
`--phase imdb` catalog as the 2026-08-03 gate — `popularity` NULL on 100% of
rows, `vote_count` on 538,937, all three numbers re-verified — plus a
`title_search_names` **person** arm of **10,896,525 rows over 1,191,768
titles**. The `alias` arm is **empty**; T7 owes it, so this is a union over one
of two arms and every number below says so. Box measured quiet by the harness
itself (zero foreign `pytest`, idle-sampled CPU drift **+0.0025**).

*Regeneration is verified rather than claimed*: the procedure reproduces the
gate's frame to the row — **81,054** shared lower-cased names, pools **432 /
2,532 / 7,178 / 20,520 / 17,887**, and exactly **2,993** cases. What is *not*
claimed is that the 750 sampled names are the same 750; the gate recorded its
procedure and its pool sizes but not its draw order.

| | bar | measured | |
|---|---|---|---|
| (1) tier-1 p95, `titles` only | ≤ 10 ms | **0.947 ms** | **PASS** |
| (2) tier-1 p95, union at 10.9M rows | ≤ 10 ms | **1.465 ms** | **PASS** |
| (3) tier-2 p50 | 33.6 ms ±10% | **39.59 ms** | **FAIL** |
| (4) tier-1 recall@5 | 1.9% (1.6–2.2) | **2.67%** | **FAIL** |

Bars (1) and (2) are scored on the 2,993 typo strings, which is the only
workload comparable to the gate's `0.6 / 1.0 / 10 ms` btree row — and it
reproduces it almost exactly: **p50 0.664, p95 0.947**, against a driver floor
(`SELECT 1` through the same path) of **p50 0.425**. So over a third of tier 1's
latency is the round trip, not the index.

**The union does not cost tier 1 its budget, and B2's arm stays.** 1.465 ms
against 10 ms at whole-catalog credited coverage. The plan's pre-recorded
failure consequence — narrow tier 1 to `titles` and reach
`title_search_names` from tier 2 alone — **does not fire**, and it was not
allowed to fire on a different workload than the one the bar named.

**What fails is the keystroke, and it fails on both arms.** p95 by prefix
length, at `--reps 5`, titles-only against the union:

| prefix length | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| `titles` only | **291 ms** | 51 ms | 15 ms | 5 ms | 19 ms | 14 ms | 2.0 ms | 2.3 ms |
| union, 10.9M | **2,707 ms** | 809 ms | 303 ms | 112 ms | 100 ms | 86 ms | 2.3 ms | 2.6 ms |

Tier 1 is a keystroke path from **seven characters up** and nowhere below it.
The union is 9× worse at one character and the two converge at seven. Worst
single probe measured: **`'m'` at 2,744 ms**, 78,203 `titles` rows and
1,069,834 `title_search_names` rows.

- **REFUTED — the sort is not the cost, and B2's residual-exposure argument
  names the wrong mechanism.** B2 declined an inner per-arm cap partly because
  *"an ordered one costs the same sort, so it buys nothing"*. In the plan for
  the worst probe the sort is a **top-N heapsort in 26 kB** costing
  microseconds. The cost is the `UNION`'s de-duplication — a `HashAggregate`
  at **17 batches spilling 47 MB to disk per worker** — plus a
  `title_search_names` bitmap heap scan that **goes lossy** (47,951 exact /
  66,156 lossy heap blocks, **5,664,971 rows removed by filter** to keep
  1,069,834), plus a hash join back to `titles` at 16 batches. The index probes
  themselves are 6 ms and 40 ms. **An ordered inner cap would therefore be far
  cheaper than B2 priced it**, because it would cap the HashAggregate's input
  rather than paying a sort that is already free — but that is a design change
  and this task does not tune, so it is handed on rather than made.
- **CONFIRMED — the join back to `titles` costs more than the second arm's
  index probe**, at every size measured.
- **Parallelism is not the lever, and at the worst case it does nothing.**
  Every W3 probe was also timed under `SET LOCAL
  max_parallel_workers_per_gather = 0`, verified to have landed (`Gather`
  present in the parallel plan, absent in the serial one). For the four largest
  probes serial/parallel is **0.997–1.029** — two extra workers buy nothing,
  because the work is disk-spill and heap-recheck rather than CPU. For
  mid-sized probes it is **1.94–2.11**. So the concurrency worry is real in the
  middle of the range and *absent* at the tail: the 2,744 ms is not a figure
  that degrades further when the box is busy.
- **Coverage is the lever, and the curve is steep.** At the enriched-tier size
  the plan actually contemplates — 10,000 covered titles, 89,808 rows — the
  union's one-character p95 is **489 ms** against 2,707 ms at 10.9M, and by
  four characters it is **5.5 ms**, indistinguishable from titles-only. The
  titles-only column is identical across both runs (291.23 against 290.24 ms at
  length one), which is the control that says the only variable was table size.
- **The union costs tier-1 recall, slightly.** 2.34% union against 2.67%
  titles-only, entirely in the two short bands (2–4: 1.85% against 2.87%);
  8 characters and up are identical. Person-name rows crowd the true title out
  of a five-row box.
- **Bar (4) fails and the index is not why.** 2.67% against a 1.6–2.2% window.
  Tier 1 finds a typo'd name essentially only when the edit lands on the last
  character and leaves a true prefix, so recall is a function of the sampled
  names' lengths — and the draw order the gate never recorded is the one input
  known to differ. 23 cases out of 2,993 separate the two figures.
- **Bar (3) fails and `m09a` is not why — measured, not argued.** 39.59 ms
  against a 30.24–36.96 ms window. The within-run A/B settles the attribution:
  the identical 2,993 cases with both prefix indexes present and then both
  dropped give **39.593 ms against 39.571 ms, a ratio of 1.001, with
  byte-identical recall**. So the GIN/GiST lesson — that an added index can
  silently tax the shipped path — **does not reproduce for a btree**, which is
  the thing that needed proving rather than asserting. The 6 ms against
  2026-08-03 is run-to-run, not `m09a`.
- **Tier 2's miss split moved and the half that carries the claim did not.**
  90.8% below the `%` floor / **0.0% truncated by the cap** / **0.0% dropped by
  the re-rank** / 9.2% out-ranked, against the shipped row's 82.8 / 0.0 / 0.0 /
  17.2. The two zeros hold **exactly**, so the cap and the re-rank are still
  inert; the floor/out-ranked balance shifted 8 points on a different draw of
  750 names. Tier-2 recall@5 **81.49%** against the recorded 82.5%, with the
  band curve reproducing (2–4: 32.9%, 5–7: 77.0%, 8–11: 97.3%, 12–19: 99.7%,
  20+: 100%).
- **Index build and size.** `ix_titles_name_lower_prefix` **0.666 s / 44.2 MB**
  over 1,271,138 rows, against the gate's 0.559 s / 44 MB — size reproduces
  exactly, build time is run-to-run. `ix_title_search_names_name_lower_prefix`
  **4.527 s / 155.4 MB** over 10,896,525 rows, which has no prior number
  because the table did not exist when the gate ran.

**Two things about measurement harnesses this run paid for, which are not about
search.** A quiet-check that compares the one-minute load average before and
after **condemns every clean run**, because a forty-minute run of continuous
querying raises its own average — this one went 1.34 → 2.82 on a box that was
provably idle throughout. And a foreign-process census that matches the whole
command line counts *the shell that mentions the word*: `pgrep -f pytest`
reported four processes on a box the coordinator had just measured clear, and
all four were idle `sleep 5` waiters watching for pytest. Both were caught
before they discarded a good run, both by predicting the failure rather than
meeting it. Match argv **tokens**, exclude shells, and sample CPU **drift**
between two moments when the harness itself is idle.

**And a guard is vacuous below the scale that triggers the thing it guards.**
The check that `SET LOCAL max_parallel_workers_per_gather = 0` actually removed
the `Gather` cannot fire on a 4,000-row toy catalog, because the planner never
chooses a Gather there — so a harness validated only on a fixture can carry
silently inert checks into a real run. Proven on the real statement instead: **1
Gather node without the knob, 0 with it.** Same family as this file's own
finding that every typo case in `SuggestIndexContract` is green at a trigram
floor no deployment uses.
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
(`fastembed:BAAI/bge-small-en-v1.5`). **Since 2026-08-13 the prefix is a
dispatch key as well**: `fastembed:` and `openai:` select two different
`Embedder` implementations, an unrecognised prefix raises at startup rather
than falling back, and the shipped default checkpoint is
`BAAI/bge-large-en-v1.5`. Nothing measured in this paragraph changed; see the
`m09e` section at the end of this file.
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
**`halfvec` is correct and effectively free, and numpy `float16` is
not.** Round-trip error over 1,000 vectors: max cosine error **1.21e-04**,
mean 3.03e-05 — three orders of magnitude below the useful signal, with top-1
and top-5 ordering identical in 42/42 queries. Storage at 1,271,138 titles:
1.83 GiB → 0.92 GiB. But brute-force exact cosine at 10k is **1.820 ms in
Postgres against 0.088 ms in numpy `float32`** — PRD 05's "sub-millisecond"
was a numpy figure — and numpy `float16` is **140× slower than `float32`**
(12.275 ms), because there is no SIMD GEMM path for half precision. Store
`halfvec`; convert to `float32` before any numpy dot product.
**Every number in this paragraph was taken at `halfvec(384)`, which stopped
being the width on 2026-08-13** (`m09e`, 1024). The *conclusions* are
width-independent — quantisation error is per lane, and `float16`'s missing
GEMM path is not about length — but the two **storage** figures are not: 1.83
GiB → 0.92 GiB is a 384-lane count, and 1.820 ms was measured over 384-lane
vectors. Re-measure before quoting either at 1024. See the `m09e` section at the
end of this file.
**The deterministic `FakeEmbedder` is `blake2b → Box-Muller → L2-normalise`,
and its non-vacuity is measured — at 384, which is no longer its width.** Over
15,996,000 off-diagonal pairs at `dimension=384`: cosine
mean −0.00001, **sd 0.05102 against a theoretical 1/√384 = 0.05103** (ratio
1.000), max +0.2549, **zero pairs above 0.5**. `m09e` moved `_DIMENSION` to
`EMBEDDING_DIMENSIONS` (1024) and **that run was not repeated**, so read the
numbers as a property of the construction rather than of today's default: the
mechanism is dimension-independent and a wider vector can only concentrate the
off-diagonal distribution further (theoretical sd 1/√1024 = 0.03125), which
makes the measured claim conservative at the new width rather than unverified
in the direction that would matter. **Re-run it before quoting a number.** The
fake tracking the constant is not cosmetic: `composition.embedder` now returns
`None` on a width mismatch, so a fake left at 384 would make every case
building a real `embedder()` assert against a deployment with no model — which
is how the stale literal was found, through two unit cases about
`HF_HUB_OFFLINE`. **Use `hashlib`, never
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

**And expansion was billed on searches the semantic lane cannot serve, which
this run also found — issue #16, closed 2026-08-19.** The guard was `embedder
is None`, not *"anything is embedded"*. Measured with
`USHER_EMBEDDING_ENABLED=true` and `title_embeddings` empty: `usher search
--mode fused` bought a completion, printed `expanded: …`, returned
`semantic_coverage=0.000`, and *then* printed *"no title in the filtered
population has an embedding"* — **the warning arrives after the money**, on
every fused search of every not-yet-backfilled deployment. `--mode full_text`
correctly bought nothing.

🔴 **Both halves of the reason it was left open were wrong, and the shape of
the error is what transfers.** The entry said the correct predicate — *does any
title in the **filtered** population have a vector* — *"is not answerable
before the vector that does the filtering exists"*. Nothing in a
`SearchFilters` is derived from a query vector, and `PostgresSearchIndex`'s
`_COVERAGE` already took `predicates` and no vector: **the strong predicate was
already computed, in the same class, and only its callability was missing.**
Having convinced itself the strong form was impossible, the entry then priced
the weak one (`SELECT 1 FROM title_embeddings LIMIT 1`) at *"a new
`TitleEmbeddingRepository` port method, two implementations, a contract case
and a read on every fused search"* — and paid for the weak answer at the strong
answer's price. What shipped is `SearchIndex.semantic_coverage(filters)`,
delegating to the same `_predicates`/`_coverage` pair, so the guard and the
reported number cannot drift. **Before pricing a fix as needing a weaker
predicate, look for the strong one already being computed a few lines away.**

The remaining cost — *"a read on every fused search"* — is a fact about where
the read is put rather than about having one. It sits behind `expander is not
None`, which is false on every shipped deployment, so nothing pays for it
except the deployments that were about to buy a completion, and those trade one
count over the enriched tier for one 1.4 s call.
`test_the_shipped_default_probes_nothing_before_embedding` pins the ordering,
because hoisting the probe above the expander check is the tidier-looking
version and is the one that costs everybody.

**Its position is *most* of the cost argument, and this said "the whole"
until #16.** `QueryExpansionService.expand` is called from exactly one
line -- the line before `SearchService`'s `self._embedder.embed([...])`, inside
the `else` of the `embedder is None` branch. Four things follow from the
position and each is a case: a `full_text` search buys no completion (no embed
to sit in front of), a deployment with no embedder buys none (`semantic` raises
and `fused` narrows before reaching it), a blank query buys none (refused
before the model), and **`usher suggest` buys none** -- `SuggestIndex` is its
own port with no semantic lane, which is what keeps a completion off the path a
client drives per keystroke. The fifth follows from the *probe* and no position
could have supplied it: a population with no vectors buys none. **A guard in
front of a cost says the cost is not paid on the paths that never reach it, and
says nothing about the paths that reach it and cannot benefit.** The unit of
spend is one search whose semantic lane was going to be able to answer.

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

## M9 Task S4 — the embedding path measured against a real 130,647-title tier, and the estimate `usher index` prints is the model's rate rather than the backfill's (2026-08-12)

**The first run of this subsystem over a genuinely enriched tier.** Every
figure above it was taken on a synthetic corpus or a ~10k population; S3 left
130,647 enriched titles carrying weight classes C and D, and this is what the
shipped code did with them. Host as always: Ryzen 7 5800X3D, CPU only,
`pgvector/pgvector:pg17`.

**The population, and what it is a count of.** `title_embeddings` is
**130,647 rows, every one carrying a vector, 0 refused** — against S3's frozen
`s3_tier_snapshot` of **130,806 ids**, of which 130,647 enriched. So the
embedded population is **100.0% of the enriched population and 99.88% of the
frozen tier**, and the 159-row gap is not missing embeddings: those titles are
still `skeleton`, which `_POPULATION` (`t.enrichment_state <> 'skeleton'`)
excludes, so no `index` job was ever owed for them. **The population predicate
demonstrated at scale rather than asserted**: 1,141,720 skeletons, every one
without an embedding row, and `count_stale` **0** — 1.14M rows that would match
the staleness half and are excluded by the population half.

**Two passes, and the second one's size is the measurement.** `EnrichService`
enqueues `INDEX` and `DERIVE` together at `BACKFILL` and does not order them,
so a title whose `INDEX` is claimed first embeds from a document with an empty
weight-class-B segment and goes stale the moment `DERIVE` writes
`credit_names`. Pass one drained both kinds. Pass two —
`usher index --backfill`, sweeping the whole enriched tier in **3.3 s** —
reported **8,603 stale titles swept, 8,603 index jobs written**: **6.58% of
130,647**. Not an error, and not a race that mostly loses: `DERIVE` won for
93.4% of the tier. Confirmed independently on the rows themselves — exactly
8,603 have `updated_at > created_at`. A third sweep wrote **0**, and
`usher index` bare reports **stale 0, refused 0**.

**Wall clock.** Pass one's index phase ran 00:07:46Z (the instant the enrich
queue emptied) → 02:26:19Z: **8,313 s = 2.31 h** for ~130,091 embeddings,
**15.6 rows/s aggregate** across the two surviving workers, on a host also
running other agents' test suites. Pass two, index-only and on a quiet box:
8,603 jobs in **361 s**, **23.8 rows/s aggregate**.

**The invariant holds and the backfill does not run at it — quote it for the
model, never for the queue.** Real documents, measured with the shipped
tokenizer over 1,000 sampled embedded titles: mean **125.4 tokens**, median
118, p95 197, max 323, none over the checkpoint's 512 window. So the enriched
`name + credits + overview + tagline + genres + keywords` document sits inside
the **~100–130 tokens** this file already records, and `cli.py`'s 135-token
estimate is the fair, slightly pessimistic figure it says it is. The *model*
on those documents, one text per call — the shape `IndexService._embed` uses —
runs at **9,683 tokens/s**, inside the **8,000–10,700 tokens/s** invariant. The
*queue* moved 8,603 × 125.4 = 1,078,816 tokens in 361 s: **2,988 tokens/s
across two workers, ~1,494 each — about 15% of the model's own rate.**

**So `usher index`'s "estimated worker time" is off by a measured factor, and
its arithmetic is not what is wrong with it.** It printed **109–145 s** for the
pass that took **361 s** — **2.5–3.3×**. The line computes `stale × 135 /
10,700` and `/ 8,000`, which prices *the model*; the backfill is the model plus
a claim, three reads (`titles.get`, `credit_names_for`, `embeddings.get`), a
staged `COPY` through a `CREATE TEMP TABLE ... ON COMMIT DROP`, and a commit —
**per title**. `db/repositories/search.py`'s docstring prices that staging path
"at the 2k-10k rows boundary call 4 embeds"; here it ran 130,647 times.

**A quarter of the drain was the staleness gauge.** `usher work` calls
`SearchGauges.refresh` after *every* `worker.run_once()` — a pass of at most 20
jobs — and the refresh is three counts. At this tier `count_stale` is
**360.9 ms** and `count_refused` **22 ms**. Pass two's 8,603 jobs is ~431
passes, ~215 per worker, × 0.383 s = **82 s of the 361 s wall clock, 23%**,
spent counting a backlog nothing reads while a backfill is running. S3 watched
this grow 16.4 → 29.4 → 327.9 ms as the tier went 7,718 → 18,267 → 88,001; at
130,647 it is 360.9 ms and has flattened, because the cost is the scan of the
enriched population and that population has stopped moving. `telemetry.py:546`
and `composition.py:1317` both still describe this scan's population as "2k-10k
rows".

**And an idle worker is not idle — the cost is on the database, which is why
`ps` says otherwise.** Two workers with an empty queue burned **0 seconds of
process CPU over 60 s** and simultaneously held `usher-m9-pg` at **9–67% CPU**:
each 5-second poll is another gauge refresh, and the refresh is an O(tier)
scan. Anyone measuring anything else on the same database must stop the
workers, not merely observe that they are quiet.

### The pool walk `SimilarityService.rebuild` draws — the first per-seed price in this project's history

Priced by running `rebuild`'s own page shape read-only — `list_embedded` →
`nearest_for(page_ids, limit=_CANDIDATE_POOL)` — against the real populated
table, writing nothing.

| what | measurement |
|---|---|
| 3 pages × 50 seeds | 1,902 / 1,843 / 1,859 ms → **37.36 ms/seed** |
| 1 page × 500 seeds (`rebuild`'s default) | 18,250 ms → **36.50 ms/seed** |
| `list_embedded`, 500 rows, at the 0/25/50/75/95th percentile cursors | 2.2–20.7 ms — flat, keyset confirmed |
| prefix / suffix / random 50 seeds | 38.75 / 38.58 / 38.04 ms/seed |

**Full walk: 130,647 × 36.5 ms = 4,769 s ≈ 80 minutes.** *Measured*: the
per-seed cost, over 650 seeds, at the real population, on an idle box.
*Extrapolated*: that the remaining ~130,000 seeds cost the same as the 650 —
which is the only step here that is not a measurement, and which the
prefix/suffix/random row is the check on. Page size is not a lever (the two
page shapes agree within 2.3%) and the seed-side paging is ~3 s of the 80
minutes.

**The cost is linear per seed in the population, so the walk is quadratic in
it.** Five points taken as the population grew under two-worker load: 30,562 →
13.93; 44,344 → 19.18; 58,111 → 25.50; 71,967 → 29.55; 86,868 → 34.61 ms/seed.
Fit ≈ `2.7 ms + 0.367 µs × N`, which predicts 50.7 ms/seed at 130,647 against
the 36.5 measured there on a quiet box — **a loaded host overstates this by
~39%, so a price taken during a drain is not the price**.

**Bounding the walk by seed count is legitimate; bounding it by a
`list_embedded` prefix is not.** The price is seed-independent to within 1.9%,
so a random sample of seeds costs the same per seed and gives an unbiased
estimate of pool composition. A prefix does not: `list_embedded` orders by
`title_id`, the ids are UUIDv7 minted during a bulk import that walked IMDb
`tconst` order, so a prefix is ordered by registration era.

**A figure this project already had, in the place nobody looked.** The M9 plan
states that "165.7–166.2 ms for 50 seeds and 619.9 ms for 200" appears nowhere
in this repository. It appears twice — `db/repositories/search.py:263-264` and
`tests/integration/test_services_similar.py:711-712` — and the plan's real
point survives intact, because **neither site records the embedded population
it was taken over**. Both are measuring where the `genome_scores` join belongs
relative to `_EXACT_SCAN_OFF`, on a "real 15,565-row" genome table; the 50-seed
baseline is a per-seed cost of ~3.3 ms, which is **11× cheaper than the same
statement over 130,647 embeddings** for exactly the reason the linear fit
above gives. A per-seed price without its population is not a price.

### The mutation ledger for `test_a_title_embedded_before_its_credits_landed_is_stale_again`

The shipped `_FINGERPRINT_SQL` already satisfies the new case, so its red was
demonstrated rather than claimed. Two plants, harness outside the tree at
`/var/tmp/m9-S4/plants.py`, each asserted present before the run that judged it
and each restore verified by `md5sum` against `f804193a097e3b9dad7066c5c657a53d`.

| plant | spelling | died on |
|---|---|---|
| careless | `usher_array_text(t.credit_names) \|\| CHR(10) \|\|` deleted | the **premise** — `assert True is False`; six SQL segments against the composer's seven, so no uncredited title agrees either |
| careful | the same line replaced by `'' \|\| CHR(10) \|\|` | the **named assertion** — `assert False is True`; seven segments with a permanently empty third, so the premise passes and the credit write moves nothing |

The careful spelling is the one worth keeping in mind: it preserves everything
the case checks first and fails only the property the case is named for, which
is the shape a linter and a careless plant both miss.
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

🔴 **The ceiling survives issue #25 unchanged, and the section below is what it
was protecting the wrong row from.** Nothing in the table above moved: the
bound is still `0.70` against `0.35 + 0.15 + 0.15 + 0.02 + 0.02 + w`, the
margin is still **0.009615** over all five non-relevance signals, `w` is still
0.005, and `test_no_combination_of_the_other_five_can_displace_an_exact_match`
is unedited. What changed is *which* hit occupies dense rank 0 — see the next
section — which is the one thing this bound never said anything about. **Read
those two sections together before touching either**: capping the relevance
decay, the obvious alternative fix, is exactly the change that would have
invalidated this table, and it was declined for that reason among others.

## An exact name match now leads the lexical lane, and 29.3% of the catalog could not be found by its own name (2026-08-19, issue #25)

**The defect, reproduced byte for byte through the live API** at 1,272,866
titles: `GET /search?q=The Matrix` put the 1999 film **5th** at
`0.35007068764143234`, behind three 2018 video essays at `0.8032` that carry no
popularity at all. Both scores reproduce from the shipped constants, and
popularity was *applied and helped* — dropping it scores the film 0.2729. The
mechanism is rank-0 dominance, which is the section above working as designed
over a lexical lane that had the wrong row at rank 0: `ts_rank_cd` rewards a
document that repeats the query and cannot know that the query **is** a title's
whole name.

**It generalises, and this is the number that says so.** The issue itself named
the sampleable version — *"how often a title's exact name is outscored by a
longer document repeating it"* — and it had never been run. 800 titles drawn
from the live catalog (400 `skeleton`, 400 `enriched`, frozen to a file so both
arms are paired), each queried by its own name through the shipped path at
`mode=full_text, limit=20` with the singleton household:

| | before | after |
|---|---|---|
| **miss rate, pooled (n=800)** | **38.4%** (307) | **20.8%** (166) |
| skeleton (n=400) | 29.5% (118) | 15.8% (63) |
| enriched (n=400) | 47.3% (189) | 25.8% (103) |
| — of which **outranked** (retrievable, uniquely named, lost anyway) | **234** | **0** |
| — of which **namesake** (rank 1 carries the identical name) | 62 | 155 |
| — of which **not retrieved** (own name does not match own document) | 11 | 11 |

Transitions, per title: `hit → hit` **493**, `outranked → hit` **141**,
`outranked → namesake` **93**, `namesake → namesake` 62, `not_retrieved →
not_retrieved` 11. **Zero regressions** — no title that was rank 1 stopped
being rank 1 — and `outranked → outranked` is empty. Bar written first at
`/var/tmp/usher-i25-bar/BAR.md`, `sha256
4729d7eb2c7491b94e9029ee554b8e92ec0cfde7753cc92687f106f92d28d21f`; sample
`sha256 3a03cfeeea65de38f4c7134c6734694b60a76d5b1259216fe5a6191736036d73`;
harness `scripts/measure_exact_name_rank.py`, read-only through
`postgresql_readonly=True` and assembled with no `SearchAnalytics` so a
measurement writes no `search_queries` row.

**The enriched tier is the *worse* half, which inverts the intuition.** 47.3%
against skeleton's 29.5% before, 25.8% against 15.8% after — enriched titles
carry overviews, taglines, keywords and credit names, so there is far more
document for a query to match twice, and far more competition from other
enriched rows. A fix aimed only at the long tail would have missed the tier
where the miss rate is highest.

**Why candidate 1 and not the other two**, in the issue's own numbering. (2)
Capping the relevance decay converts rank-0 dominance from exact to
contestable, which invalidates the taste-weight ceiling above — a bound argued
to four decimal places, in exchange for making *every* strong match
displaceable by popularity, which is the failure this project already refused
when it kept popularity a hard key above `vote_count` in the suggest statement.
(3) Breaking ties inside the lexical lane is narrower than the defect: the
essays do not tie *The Matrix*, they **outscore** it, so a tiebreak never runs.

**Two halves, and each kills a different failure.** The SQL key (`lower(t.name)
= lower(btrim(:query))`, ordered ahead of `score` in `_FULL_TEXT` and in
`_FUSED`'s lexical CTE) is what gets the row into the candidate window at all —
it is ahead of the `LIMIT`, and no re-weighting can rank a row the lane never
returned. The service key (`_dense_ranks` grouping on `(exact_name, score)`) is
what makes rank 0 a group of **one**: `ts_rank_cd` ties are pervasive — the
tie-group-of-498 figure this file already carried — and a shared rank 0 cancels
the relevance term and hands the decision straight back to popularity, which is
the same defect wearing a different hat. Shipping either half alone leaves a
measurable hole.

**The prefix half of tier 1's signal was declined, and the argument is the
defect's own shape.** Tier-1 suggest computes `lower(name) LIKE 'typed%'` and
already ranked this query correctly, which is what identified the signal — but
the three essays are *themselves* prefix matches of `The Matrix`, so a prefix
key flags all four rows alike and separates none. It also costs something real
in the other direction (every `Matrix Warrior` above `The Matrix` on the query
`Matrix`) with nothing measured behind it. On tier 1 the whole candidate set is
prefix matches, so the key is a filter and popularity does the ordering; in the
search lane the set is mixed. Equality is the part of the rule that transfers.
`title_search_names` is not joined either — 10.9M person rows and an alias
table, a second scan on the one statement with a latency figure to keep — and
that is the obvious next measurement rather than an omission.

**Latency: the paired measurement is the honest one, and the arm-against-arm
number failed the bar.** Scored as written (after p95 ≤ 1.25 × before p95 over
the two whole-service arms) it is 19.31 → 24.42 ms = **1.265×**, a fail by
0.28 ms — and the two arms ran twenty minutes apart on a box running fourteen
containers. Running the **two statements** back to back over the same 800
names, alternating which goes first: old p50 0.596 / p95 13.42 ms, new p50
0.627 / p95 14.84 ms = **1.106×**, paired delta p50 **+0.013 ms**, mean +0.099
ms, and the new statement is *faster* on 357 of 800 queries. `EXPLAIN` confirms
there is no plan change to explain — Bitmap Index Scan → Bitmap Heap Scan →
Sort → Limit on both, differing by one sort key. **A p95 taken from two runs
minutes apart on a shared box measures the box as much as the change**; an
interleaved pair is what the difference is small enough to need.

### Two ways a title cannot be found by its own name, both unfixed and neither a ranking problem

The 11 `not_retrieved` misses are the same 11 before and after, and they split
into two shapes worth knowing before anyone reads the residual rate as ranking
quality:

- **`websearch_to_tsquery` reads ` - ` as negation.** `Regret - Cherie Laurent`
  compiles to `'regret' & !'cheri' & 'laurent'` — the title's own name excludes
  the title. `Die 90er - Jahrzehnt der Chancen` → `'die' & '90er' &
  !'jahrzehnt' & 'der' & 'chancen'`. A hyphen surrounded by spaces is ordinary
  punctuation in a film title and a **NOT operator** in the websearch grammar,
  and 8 of the 11 are this. It is the reason to be careful about `plainto_` vs
  `websearch_to_` rather than a defect in either.
- **A name of nothing but stop words** produces an empty tsquery and a `NOTICE:
  text-search query contains only stop words`. `In Between` is a real title in
  this catalog and matches nothing, including itself.

Both are **retrieval** failures — the row is not a candidate, so no ranking
change reaches them — and both would be answered by the same thing: a name
arm that does not go through the English text-search parser at all. Not built
here; the bar declared them out of reach before the run rather than after.

## The two-tier suggest reached a request boundary, and the boundary is where the keystroke defect is answered (2026-08-12, M9 B5)

B3's curve above is the measurement; this is what was done with it, so that a
reader who opens `services/search.py` or `adapters/search/` does not have to
find [ADR-0031](../../docs/prd/decisions/0031-the-two-tier-suggest.md) to learn
the shape. **No statement changed** — no floor, no cap, no index, no `UNION`.

- **`GET /search/suggest?q=&tier=prefix|fuzzy&limit=`**, one route, defaulting
  to `prefix`, echoing the tier that answered. `SearchService` holds **both**
  `SuggestIndex` implementations as required collaborators, named
  `prefix_suggestions`/`fuzzy_suggestions` rather than positioned — two
  adjacent parameters of one type are a swap that answers plausibly either way,
  and only a case asserting a typo is *absent* from tier 1 can tell.
- **The route does not run tier 1 below a four-character prefix**, and four is
  derived from the curve rather than chosen: it is the shortest length at which
  tier 1's p95 (112 ms) is below tier 2's (211 ms), which is the property the
  whole split rests on. At three characters tier 1 is **303 ms** and therefore
  slower than the tier it exists to be cheaper than. Not the 10 ms bar, which
  would set the minimum at seven; not a `Settings` field, because the number is
  a function of catalog size rather than of an operator's preference.
- **Tier 2 is bounded at one character only**, because nobody has measured the
  trigram statement *per prefix length* — its 33.6 ms p50 / 211 ms p95 /
  730 ms max are whole-name figures, exactly as tier 1's 0.6 ms was before B3
  re-measured it per length. A bound with no measurement under it is the shape
  `ports-and-error-taxonomy.md` records. Its defence is the client's debounce;
  **the server debounces nothing**.
- **`usher suggest --tier` defaults to `fuzzy` where the route defaults to
  `prefix`**, and `SearchService.suggest` takes `tier` as a required keyword
  with **no default at all**, so neither boundary inherits the other's answer.
  A route is driven per keystroke and a command is typed once.
- **The ordered inner per-arm cap is still not made and is the first thing a
  follow-up should measure.** G7 is refuted (the sort is a 26 kB top-N
  heapsort; the cost is the `UNION`'s de-duplication spilling 47 MB and a lossy
  bitmap heap recheck), so it is far cheaper than B2 priced it — but changing
  the statement would leave B3's per-length curve describing a query that no
  longer exists, and B5 ships no SQL.
- **What is still not measured**, beyond the list this file already carries:
  tier 2 per prefix length; the four-character minimum against real typed
  queries, which is `search_queries` and has no rows until after M9; and the
  curve over a `title_search_names` that carries T7's **alias** rows as well as
  the 10.9M person rows it was taken over — the shipped table is larger than
  the measured one, so the curve is optimistic in the direction that matters.

## What one `search_queries` row costs, and the plan's own estimate of it was an order of magnitude out (2026-08-12, M9 F2)

**Measured once, on a throwaway `pgvector/pgvector:pg17` container of this
driver's own** — never `usher-m9-pg` or `usher-b3-pg`, which other agents hold
— with the real migration chain (`alembic upgrade head`, `m09c`) and a warm
connection, 2,000 iterations each. Driver outside the working tree at
`/var/tmp/m9-F2/`.

| | p50 | p95 | p99 | max |
|---|---|---|---|---|
| `record()` **+** `commit()` — what F2 adds per answered search | **3.957 ms** | 4.738 ms | 7.117 ms | 10.194 ms |
| `record()` alone (SAVEPOINT + INSERT, no commit) | **0.909 ms** | 1.078 ms | — | 2.537 ms |
| `commit()` alone | **2.965 ms** | 3.520 ms | — | 11.004 ms |
| floor: `SELECT 1` + `commit()` on the same connection | 0.549 ms | 0.658 ms | — | — |

**F2's acceptance said the write sits "on a path whose p50 is two orders of
magnitude larger" and that is wrong by a factor of ten.** Against this file's
recorded full-text figures — p50 **33.3 ms**, p95 **208.8 ms** over 2,993 cases
at 1,271,138 titles — the write is **11.9% of a p50 search** and 2.3% of a p95
one: **one** order of magnitude, not two, and at the median it is an eighth of
the search rather than a hundredth. The conclusion the estimate was supporting
survives (no bar is minted, and none is needed), but a reader pricing a future
change off *"two orders of magnitude"* would be out by 10×.

**Three quarters of it is the commit, not the INSERT**, and that is the part
worth carrying: `search_queries` has no index beyond its primary key, so the
INSERT itself is 0.9 ms and the rest is one WAL flush. Two consequences.
`usher search` had no commit at all before F2, so it pays the whole 4 ms. On
`GET /search` the request *did* already commit through `api/deps.get_session`
— but on a read-only transaction that commit flushes nothing, so the marginal
cost on the route is the same ~3.4 ms above the floor rather than the INSERT
alone. And anything that later records a *keystroke* multiplies this by the
suggest path's rate, which is the volume half of PRD 10's argument for why
`GET /search/suggest` writes no row.

Caveats, stated because they bound the number rather than decorate it: a fresh
container with `fsync=on` and an empty table, on an otherwise-idle host, with
one connection and one prepared statement — so this is the steady-state cost of
a warm path and not a first-call or a contended one, and the INSERT figure is a
property of a table with no secondary index that a later index would move.

## `bge-m3` over HTTP, the width move, and two vLLM flags that each cost a run (2026-08-13, `m09e`)

The design argument is
[ADR-0038](../../docs/prd/decisions/0038-the-embedding-width-is-deployment-wide-ddl.md);
this is the evidence and the deployment facts, including the ones that are about
somebody else's process and therefore have nowhere else to live.

**`fastembed` does not ship `BAAI/bge-m3`, and this was enumerated rather than
assumed.** All five model classes on fastembed 0.8.0 — `TextEmbedding`,
`SparseTextEmbedding`, `LateInteractionTextEmbedding`, `ImageEmbedding`,
`LateInteractionMultimodalEmbedding` — listed and searched; no `bge-m3` in any.
That is the whole reason a second `Embedder` exists. It is not a judgement about
in-process versus remote, and anyone re-opening the question should re-run the
enumeration against the current fastembed before assuming it still holds.

**The served model's norm is exactly 1.0.** Checked live against the reference
endpoint, so `_NORM_TOLERANCE = 1e-4` carries the same four orders of magnitude
of headroom here as it does for `fastembed` against the 8.99–9.46 a missing
`Normalize` module produces. `EmbedderContract` covers this *less* well for the
remote runtime than for the local one: the served model is the one thing about
`OpenAICompatEmbedder` that can change while the process lives.

### The serving topology — two vLLM engines on one 4090, and why the big one survives

The host now runs both models at once: `cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit` at
`--gpu-memory-utilization 0.76` on **:8000** (`USHER_LLM_BASE_URL`) and
`BAAI/bge-m3` at **0.11** on **:8001** (`USHER_EMBEDDING_BASE_URL`). Measured
weights **16.01
GiB** and **2.11 GiB** against **24,564 MiB** total, with **~2,135 MiB** held by
the desktop session.

**The 26B fits beside an embedding model because of its attention shape, not
because of slack.** It is 30 layers of which only **5 are full-attention** — the
other 25 are sliding-window at 1024 — so one 16K-token sequence costs **435 MiB
of KV**, not gigabytes. vLLM's own report at that utilisation: *Available KV
cache memory* **0.77 GiB**, *GPU KV cache size* **6,688 tokens**, *Maximum
concurrency for 16,384 tokens per request* **1.19x**. That last figure is the
one to read before adding a third tenant: the chat engine has room for
approximately one full-length request at a time and no more.

### `--load-format pt` is a trap for this checkpoint, and it fails silently

`BAAI/bge-m3` ships `pytorch_model.bin` and **no safetensors**, which looks
exactly like a model that needs `--load-format pt`. It is not.
`LoadFormat.PT` globs `["*.pt"]`, so on this repository it matched only
`colbert_linear.pt` and `sparse_linear.pt` — two auxiliary heads — and loaded a
model with **391 uninitialised weights**. The default `auto` globs
`["*.safetensors", "*.bin"]` and is the correct setting. **Nothing raises**: the
server starts, answers, and returns vectors off a randomly-initialised backbone,
which is `ADR-0022` Part 3's failure family (a plausible ranking that is wrong
everywhere) arriving through a loader flag instead of a missing module. The
operator's vLLM compose file carries the explanation at the flag —
`~/anirudhlath/vllm/docker/compose.yml`, **not a file in this repository; do not
edit it**.

### `--served-model-name` must be the checkpoint, and the taxonomy was right while the message was useless

With `--served-model-name bge-m3`, **every `index` job parked** on
`PortDataMalformed: the embedding endpoint rejected the request with HTTP 404`.
Usher sends `checkpoint_of(model_name)` — `BAAI/bge-m3` — and vLLM answers 404
for a model it does not serve under that name. So the alias has to be the
checkpoint, because the request body is derived from the same string the
fingerprint stores and the two cannot be decoupled without decoupling the
fingerprint from the model.

**Two things worth separating in that outcome.** The error taxonomy behaved
*correctly*: a non-429 4xx is permanent, retrying it cannot help, so parking
rather than retrying is the right call and it is what happened. But
`_ENDPOINT` is a constant precisely so no message carries a URL or a credential
(`ports-and-error-taxonomy.md`'s rule, and `OpenAICompatibleClient`'s
precedent), which means the message named **neither the model nor the URL** and
the diagnosis came entirely from the server's own logs. That is the accepted
cost of the redaction rule and not a defect — but it means **a 404 from this
adapter is a signal to go read the inference server**, and that is worth
knowing before spending time on the client.

### `alembic upgrade head` bypasses the settings scrub

Found here, recorded in `config-cli-and-deployment.md` because it is a config
finding rather than a search one: with `USHER_DATABASE_URL` absent, alembic
prints pydantic's raw `ValidationError` — `input_value={…}` and a truncated
`secret_key` with it. `cli._settings_problem` exists to scrub exactly that and
alembic's `env.py` never reaches it. **Found, not fixed.**

### The live database, immediately before and after `m09e`

| | before (`m09d`) | after (`m09e`) |
|---|---|---|
| `title_embeddings.embedding`, `user_taste.centroid` | `halfvec(384)` | `halfvec(1024)` |
| `title_embeddings` rows | **130,673** | 0 |
| `ix_title_embeddings_hnsw` | **146 MB** | recreated empty |
| `title_embeddings` total relation | **278 MB** | — |
| `title_neighbors` rows | **3,266,175** | 0 |

The index is rebuilt at the **same** `m = 16, ef_construction = 64` and the same
`WHERE embedding IS NOT NULL` predicate, so nothing about the graph's shape is a
variable in whatever the new size turns out to be — only the lane count, which
doubled.

### The ranking baseline, and three measurements that are owed rather than unknown

**What the 384-lane default actually delivered, measured over this catalog with
`fastembed:BAAI/bge-small-en-v1.5`.** A plot-description query lands the correct
title in the top **0.05–0.3%** and **usually outside the top 20** — *"a man
relives the same day over and over"* ranked Groundhog Day **64th**, Shawshank
**208th**, The Matrix **262nd**, WALL-E **338th** — while a query naming a
title's subject matter directly ranked Jurassic Park **1st** and a Harry Potter
query **4th**. **The percentages are of the embedded population** — the enriched
tier, ~130k rows, not the 1.27M catalog — which is what makes them damning
rather than impressive: read the shape rather than any single rank, **topic
retrieval works and plot retrieval does not**, and a rank of 64 is a lane that
is functioning and is nowhere near a five-row box. This is the first
relevance evidence this project has for the semantic lane; ADR-0022's *"relevance
is not measured at all"* is annotated accordingly.

### The run finished, 2026-08-13, and the model is not what was wrong

130,720 titles re-embedded through `openai:BAAI/bge-m3`, 0 refused, 08:15:32Z →
10:01:27Z. The three numbers that were owed above, plus two nobody asked for and
one that changes an operator's day.

**Throughput: 105.9 minutes, 20.6 rows/s — and the GPU bought the backfill
nothing.** S4 measured the *CPU, in-process* fastembed backfill at 23.8 rows/s
aggregate. Moving the model onto a 4090 and reaching it over HTTP made the
backfill **slower**, and the reason was visible the whole time: sampled
mid-run, **GPU utilisation 6% and `usher-postgres-1` at 56% CPU**. S4 had
already priced the per-title work — a claim, three reads, a staged `COPY`
through a temp table, and a commit — and said the queue runs at ~15% of the
model's own rate. It still does; the model just got faster at the part that was
never the bottleneck. **Do not quote a GPU embedder as a backfill improvement.**

**Where the GPU does pay is the query, which is the workload that has a user
waiting.** Single-text embed against the running endpoint, 30 calls, measured
*while the backfill was saturating it*: **p50 5.7 ms, p95 9.9 ms, max 13.2 ms**.
For scale, `/search` was 13.9 ms end to end in M9's live demo, so a 568M
multilingual model adds about 40% to a search and not a multiple.

**Storage at 1024 lanes, against the same table at 384 the day before:**

| | 384 (130,673 rows) | 1024 (130,720 rows) | ratio |
|---|---|---|---|
| `ix_title_embeddings_hnsw` | 146 MB | **340 MB** | 2.33× |
| `title_embeddings` total | 278 MB | **707 MB** | 2.54× |

Both below the 2.67× the lane count alone would predict, so the graph and the
row headers amortise a little. Same `m=16, ef_construction=64`, same partial
predicate.

### Ranking: four of six improved, one collapsed, and the diagnosis is the document

Same six queries as the baseline, same SQL, only the embedder moved. Ranks are
positions in the 130,720-row embedded population.

| case | 384 | 1024 | |
|---|---|---|---|
| WALL-E | 338 | **20** | +318 |
| The Matrix | 262 | **50** | +212 |
| Shawshank | 208 | **187** | +21 |
| Jurassic Park | 1 | **1** | held |
| Potter | 4 | **3** | +1 |
| Groundhog Day | 64 | **416** | **−352** |

⚠️ **Six queries is not a relevance measurement and this table must not be
quoted as one.** It is a before/after on a fixed case set that was written
before `bge-m3` was under consideration — so it is at least not selected to
flatter the new model — and that is the whole of its authority. It is the same
thinness the query-expansion run above carries, and the same rule applies.

🔴 **The Groundhog Day collapse was chased and it is not a model defect. The
composed document is.** Its top 10 under the new model is noise (`Bad (2025)`,
`There is a Monster (2024)`, `The Old Barber (2006)`) in a band of 0.54–0.58
while the right answer sits at 0.4778. Then, embedding the *segments* rather
than the document:

| what was embedded | cosine |
|---|---|
| the shipped composed document | **0.4779** ← reproduces the stored vector to 1e-4 |
| its overview alone | **0.5936** ← would rank **1st** |
| name + overview | 0.5825 |

The composer's own fingerprint (`546e12c2161166b2f04a707aefbbecfa`) was
reproduced before any of this was believed, so the 0.4779 is the shipped path
and not a reconstruction of it.

**And the segment responsible is `credits`, on every case, without exception.**
Re-composing each gold title with `credits=()` and changing nothing else:

| case | shipped | no credits | delta |
|---|---|---|---|
| Potter | 0.5360 | 0.6302 | **+0.094** |
| Groundhog Day | 0.4779 | 0.5498 | **+0.072** |
| The Matrix | 0.5238 | 0.5839 | +0.060 |
| WALL-E | 0.5187 | 0.5762 | +0.058 |
| Shawshank | 0.4799 | 0.5284 | +0.049 |
| Jurassic Park | 0.5935 | 0.6161 | +0.023 |

**6 of 6, and that consistency is worth more than the six-query rank table
above** — a mixed result across cases is a draw, a unanimous one across the
same cases is a direction. Twelve actor names, third of seven segments and
sitting *between* the title and the plot summary, are dead weight for a plot
query and drag the whole vector toward a region full of thin, generic titles.

**The limit of that table, stated because it is easy to over-read:** only the
*gold* title was re-composed, so the cosines are compared against a corpus that
still has credits in every other document. Re-embedding all 130,720 without
credits would lift the whole distribution, so it predicts a **direction**, not a
rank. So the population was rebuilt and the ranks measured.

### The whole tier re-embedded without credits, and the answer is "do not"

`title_embeddings_nocredits`, all 130,720 rows, same model, same
`compose_document` with `credits=()`, built and dropped the same afternoon.

| case | 384, credits | 1024, credits | 1024, no credits |
|---|---|---|---|
| Groundhog Day | 64 | 416 | **87** |
| Shawshank | 208 | 187 | **61** |
| The Matrix | 262 | 50 | **46** |
| WALL-E | 338 | 20 | **3** |
| Jurassic Park | 1 | 1 | **1** |
| Potter | 4 | 3 | **1** |

On plot queries it is exactly what the cosine deltas predicted: five improve,
one is already at the ceiling, none regresses. Rank sum **677 → 199**, worst
case **416 → 87**.

🔴 **And then the counter-case, which is the whole finding.** Credits are in the
document so a person query has a semantic lane, and that was tested rather than
assumed:

| query | gold title | with credits | without |
|---|---|---|---|
| *"a Bill Murray comedy"* | Groundhog Day | **14** | **34,077** |
| *"a film starring Keanu Reeves"* | The Matrix | **17** | **46,302** |
| *"directed by Steven Spielberg"* | Jurassic Park | 4,629 | **91,419** |

Three orders of magnitude. **The credits segment is load-bearing for person
retrieval and actively harmful for plot retrieval, and no single document can be
both.** `compose_document` is not carrying a mistake; it is carrying a
compromise nobody had priced, and both sides of it are now priced.

🔴 **The escape hatch this file proposed one paragraph earlier is refuted, by
the measurement it asked for.** The argument was: removing credits is safe
*because* `search_document` carries `credit_names` at weight class B (0.396), so
the lexical lane answers person queries and RRF fuses the two. It does not.
`websearch_to_tsquery('english', 'Bill Murray')` over the shipped
`search_document` matches 122 titles and its top three by `ts_rank_cd` are
**The Bill Murray Stories**, **Biography: Bill Murray** and **Saving Bill
Murray** — documentaries *about* him, none of them films he is in. The mechanism
is the weighting the class B measurement already records: name is **0.991** and
`credit_names` is **0.396**, so a title with the person's name in its *title*
outranks every film they acted in. Same shape for Keanu Reeves.

**So neither lane answers "films with Bill Murray" without the credits segment
in the embedding**, and the plausible-sounding division of labour — lexical does
people, semantic does plots — is false in this schema. It was written into this
file as *"an argument, not a measurement"*; it is now a measurement, and the
argument was wrong.

**What this leaves, and it is a design question rather than a fix:** the two
retrieval modes want different documents, which is what a second vector per
title, a query router, or an intra-document weighting exists for. None is built
and none should be guessed at from six queries. What is settled is that
`compose_document` stays exactly as it is until one of them is, and that the
next person to notice plot retrieval is mediocre should read this section before
deleting the credits line.

🔴 **And one number an operator has to know before running anything:
`usher similar --rebuild` went from 80 minutes to 21.6 hours.** The shipped
`TitleEmbeddingRepository.nearest_for` deliberately runs under
`_EXACT_SCAN_OFF` — `enable_indexscan = off`, `enable_bitmapscan = off` — so it
is an exact scan of the whole table per seed and the HNSW index is not involved.
Measured on three consecutive warm pages of 50 seeds: **592.7 / 600.4 / 594.7
ms/seed**, median **594.7**, against S4's **36.50** at 384. That is **16.3× for a
2.67× wider vector**, which is superlinear and is the interesting part: the
exact scan's working set went 278 MB → 707 MB, past both `shared_buffers`
(128 MB) and this host's 96 MB L3, so the walk stopped being arithmetic-bound and
became memory-bound. At 130,720 seeds the full walk is **21.6 hours**.

⚠️ **The first attempt at that number was wrong by 16× and the error is worth
recording.** A hand-written `ORDER BY embedding <=> … LIMIT 200` measured
**3.20 ms/seed** and was very nearly reported as an 11× *speedup*. It was not
the same query: with no GUCs set, Postgres served it from the HNSW index, while
the shipped call forces an exact scan. **A per-seed price taken from a query you
wrote yourself is a price for a query nobody runs** — drive the repository
method, exactly as S4 did.

### And 6.1× of that 16.3× was TOAST, not the width (`m09f`, same day)

The superlinearity was the tell and it had a cause. `EXPLAIN (ANALYZE, BUFFERS)`
over ten seeds read **10,061,071 buffers against a 90,000-page table** — 11×
amplification — because `pgvector` declares `halfvec` storage **EXTERNAL** and a
1024-lane value is **2,052 bytes** against `TOAST_TUPLE_THRESHOLD`'s **2,032**.
At 384 lanes a vector was 772 bytes and sat inline; at 1024 every one moved
out-of-line. Measured on the live table: `title_embeddings` was **17 MB of heap
pointing at 340 MB of TOAST**.

**So the width did not make the vectors 2.67× more expensive to scan; it pushed
them over a threshold and made each one a TOAST index descent plus a heap
fetch, per row, per seed.** `m09f` sets `PLAIN` on all three `halfvec` columns
and rewrites. What it bought, on the shipped `nearest_for`:

| | ms/seed |
|---|---|
| EXTERNAL, as `m09e` left it | 594.7 |
| PLAIN | **95.7** |

**6.2× on the component and 6.1× on the whole job, measured on the completed
walk.** `usher similar --rebuild` ran 2026-08-13 18:31:26Z → 21:51:07Z:
**130,720 seeds, 3,268,000 rows, 11,981 s — 3.33 h at 91.7 ms/seed**, against
561 ms/seed before the revision.

⚠️ **This entry said "4.4× on the job" for the first three hours of that run and
that number was an artefact of when it was taken.** It came from the first 128
seconds — 1,000 seeds at 128 ms/seed — and was written up with a conclusion
attached: *"a component speedup is not a job speedup and this one differs by
40%."* The completed walk refutes it. Steady state is 91.7 ms/seed, the job
speedup equals the component speedup to within 2%, and the per-seed work the
scan was supposedly hiding is ~4 ms rather than ~32. **A rate taken from the
first two minutes of a three-hour job is a measurement of its start-up**, and
the reasoning built on top of it was the confident part.

Two more things fell out of it.

🔴 **`genome_scores.relevance` is `halfvec(1128)` = 2,260 bytes and has been
TOASTed since `ffa` shipped it** — 1,544 kB of heap against **41 MB** of TOAST.
Every genome-similarity number this project has ever taken was taken through a
TOAST fetch. Nothing is known to be wrong with them; they were simply never
taken any other way. `db/models/taste.py` records the value's size (*"1,128
halfvec lanes is 2,256 bytes plus a header"*) without drawing the conclusion,
which is how a measured fact sits next to its own consequence for two
milestones.

**And `PLAIN` introduces a ceiling that `EXTERNAL` did not have**: it forbids
out-of-line storage outright, so a value that will not fit in an 8 kB page makes
the *insert fail* rather than spill. That caps `EMBEDDING_DIMENSIONS` at roughly
**4,000 lanes**. Recorded on the constant. A wider model needs `MAIN`, which was
**not measured**.

### What the rebuilt neighbours actually look like, and the one title that fails both lanes

Spot-checked on the completed table, 25 neighbours per title over all 130,720:

| seed | its first five neighbours |
|---|---|
| The Matrix | Robot Apocalypse, Matrix Resurrections, Matrix Revolutions, Matrix Reloaded, The Code Conspiracy |
| WALL·E | The Clockwork Girl, My Robot Friend, A.R.C.H.I.E., Eve of Destruction, Wired to Kill |
| **Groundhog Day** | **Return to Yesterday, Snowy with a Chance of Christmas, Moving Day, Derby Day, Homesdale** |

Two of three are good — sequels plus genuine thematic matches, and every one of
WALL·E's five is a robot film. **The third is the same title that collapsed from
64th to 416th on its plot query, and the mechanism is the same one.** Its raw
embedding nearest neighbours, blend excluded, are *Perfect Day*, *Day of the
Outlaw*, *The Last Day of Summer*, *Gideon's Day*, *Martin's Day* — **seven of
eight carry "Day" in the title**. So this is not the blend and not
`title_neighbors`; it is the composed document's name segment dominating, on a
title whose name is two common words.

**State it narrowly, because the first reading of that one row was "the
neighbours are bad" and two more seeds refuted it.** The similarity lane is
good in general and degrades for titles whose names are ordinary vocabulary —
which is a *selection* effect on which titles are affected, not a defect in all
of them. It is the same finding as the credits ablation from the other
direction: what dominates one of these documents is not its plot.

⚠️ **A rate measured over a window is wrong when the writes are batched, and
this one produced 2.78, 8.33 and 21.85 seeds/s for the same run.**
`title_neighbors.computed_at` is `now()`, frozen per transaction, so all 500
rows of a page share one timestamp: a short window catches a whole batch or none
of it, and `count(distinct seed) / window` counts work done *before* the window
opened. `max(computed_at) - min(computed_at)` over N batches spans **N−1**
intervals, not N. Anchor the rate to the run's own start instead — 1,000 seeds
128 s after launch is 128 ms/seed and does not depend on where the window falls.

### The follow-up this change identified — closed 2026-08-13

**`blend_fingerprint()` does not cover the embedding model, so a model swap
leaves every `title_neighbors` row reading as current.** It hashes `_WEIGHTS`,
`_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL` — what a score *means* in the
blend's terms — and the model that produced the vectors underneath is not one of
its inputs. So after a swap every row is in `[0, 1]`, carries a plausible
`rank`, and was derived from a model the deployment no longer runs, with
`usher.similarity.neighbors.stale` reading **zero** throughout. This is exactly
the failure `blend_fingerprint` was added to close, arriving through the one
input it does not hash.

✅ **Closed the same day.** `blend_fingerprint(*, embedding_model: str)` hashes
the model alongside the three constants; `SimilarityService` takes the *name*
rather than an `Embedder`, because it reads stored vectors and a request must
never load a model. Two guards, both planted and watched to fail with the key
removed from the payload: two different checkpoints must not agree, and
`fastembed:X` must not agree with `openai:X` — the runtime is in the string for
the same reason `model_name` records it, a measured 1.41e-03 max pairwise delta
between two runtimes of one checkpoint, 6x the halfvec quantisation error.

⚠️ **The licensing case for M7's `78900b2b…` digest could no longer call the
function, and that is a shape worth recognising.** It used to monkeypatch
`_WEIGHTS` and assert the function reproduced the literal; adding a key changed
the payload's *shape*, so no arguments can produce a three-key digest any more.
It now reconstructs the historical payload explicitly and asserts the current
function does **not** answer it — which says in code that the superseded digest
came from a serialisation this project no longer performs. **When a digest gains
an input, every test that licenses an older digest by calling the current
function silently becomes unsatisfiable rather than wrong.**

**Applied to the live catalog without a rebuild, because the preconditions were
provable.** Adding an input moves the digest —
`78f3ecd20e654c0f6aa4bdf646ec099b` → `a7013154c014e0ff1b60ef5d8534a115` — so
every stored row is stale on merge, which is the mechanism working. These rows
did not need recomputing: `title_embeddings` held exactly **one** `model_name`
(`openai:BAAI/bge-m3`) and `title_neighbors` exactly **one** fingerprint, so all
3,268,000 provably came from that model at this blend and were re-stamped in
place. **Only ever do that with both counts checked** — a mixed table has no
single honest label and owes the 3.3-hour walk.

⚠️ **And the verdict carries its control, which this file demanded in advance:**
`count_stale` under the running blend is **0** *and* the same call with a bogus
fingerprint answers **3,268,000**. Zero stale is also what an empty table
reports, and an empty table is what every catalog on this host held for most of
this project's life.

`m09e` empties the table, which fixes **the instance**. **The class fix is to
feed the embedder's `model_name` into `blend_fingerprint()`** — which changes
its signature and all three of its consumers (`usher similar <title id>`'s
per-title report, the `usher.similarity.neighbors.stale` gauge, and
`usher similar --rebuild`). It is **not done**. It was kept out of a width
migration deliberately: a signature change to a fingerprint function is a change
to what every stored row *means*, and burying it inside DDL is how the next
person fails to find it. Whoever takes it should note that it also makes the
model swap a *third* cause of neighbour staleness in ADR-0020's terms, beside
the blend change (closed) and *some other title was embedded since* (still
undecidable per row).

## `semantic_coverage`'s denominator is the enriched tier, not the catalog (2026-08-19)

**It reports `1.000` on a deployment where the vector lane can answer for about
10% of what the lexical lane searches, and three docstrings plus PRD 07 called
it "the fraction of the *filtered* population".** `_COVERAGE` is
`count(*) FILTER (WHERE e.embedding IS NOT NULL) / count(*)` over
`t.enrichment_state <> 'skeleton'` **and** the request's predicates; `_FULL_TEXT`
and `_FUSED` carry the predicates and **not** the skeleton restriction. So the
two lanes of one fused search do not see the same population, and the number
answers *"has the backfill drained?"* rather than *"can the vector lane see this
catalog?"*. Measured on this project's own catalog: **130,720** vectors over
**~130,647** enriched titles is `1.000`, against **1,271,138** rows in `titles`
— which is exactly what issue #31's live `usher search --mode semantic` printed
while the same catalog was 89.7% invisible to that lane.

**The number was not changed and the sentence was.** The denominator is
boundary call 4 with an argument behind it — skeletons are never embedded, so
counting them reports ~0.008 on a healthy catalog and reads as a broken
subsystem forever — and swapping a measured denominator for an unmeasured one
is not a repair. What was wrong was a field describing itself in terms of a
population it does not use, on the wire, where a client renders it.

**The general form, and it is the second time this file has recorded it:** a
ratio is only as honest as its denominator's name, and *"the filtered
population"* is the kind of name that survives review because every word in it
is true of something. Say which rows are in the bottom, at every site that
quotes the number — here that was `SearchOutcome`, `SearchResponse`, PRD 07,
the README and `usher search`'s own printed line.
## `ef_search` priced against a real index: 200 ships, the non-monotonicity did not reproduce, and over-fetch is a *filtered*-path lever only (2026-08-19, issue #32)

**The first recall figure this constant has ever had from a real index.** Every
`ef_search` number above it was taken on uniform-random 384-lane vectors at 2%
filter selectivity with `hnsw.iterative_scan` **off** — a configuration this
project has not shipped since M6.

*Sample and denominators.* 132,409 real `openai:BAAI/bge-m3` vectors
(`halfvec(1024)`, `PLAIN`, `m=16, ef_construction=64`, pgvector 0.8.6 on
PostgreSQL 17.10), the live `usher-postgres-1`, **read only** — no reindex, no
rebuild. **12 typed plot queries, embedded once through the deployment's own
endpoint and frozen**, so every condition scores the identical vectors. Gold is
the **exact top 10 per query per arm** under `enable_indexscan = off,
enable_bitmapscan = off` — the two GUCs `nearest_for` forces — so recall@10 has
a denominator of **10 per query, 120 per condition per arm**. Latency is **288
observations per condition per arm** (12 queries × 12 scored rounds × 2 runs,
round 0 discarded as warm-up), with conditions **interleaved** rather than
blocked so a burst of foreign load cannot tax one of them. Bar, sample and
decision rule were written down before anything ran, at
`/var/tmp/i32-measure/PREREGISTRATION.md`, sha256
`0be3c3504a44c11cb37484c0a0d8f4f0f792bf73025a5dfb26fc1d4bbb59ed6c`.

| `ef_search` | recall@10 unfiltered | p50 | p95 | recall@10 filtered (4.8%) | p50 | p95 |
|---|---|---|---|---|---|---|
| 40 | 0.700 | 3.21 ms | 4.75 ms | 0.733 | 16.11 ms | 57.48 ms |
| **100** — the old default | **0.858** | **4.77 ms** | **7.30 ms** | **0.783** | 18.15 ms | 58.50 ms |
| **200** — ships | **0.917** | **10.59 ms** | **16.18 ms** | **0.808** | 22.56 ms | 57.45 ms |
| 400 | 0.967 | 20.13 ms | 29.90 ms | 0.817 | 23.53 ms | 57.80 ms |
| 1000 | 0.992 | 45.46 ms | 67.50 ms | 0.958 | 49.09 ms | 67.54 ms |

Driver floor (`SELECT 1` through the same connection) p50 **0.095 ms**, so none
of this is round trip. Beside the recorded query-side budget — embed p50 **5.7
ms**, p95 9.9 ms — the shipped pair is now ~16 ms at p50 rather than ~10.5.
**400 and 1000 are refused on cost, not on recall.**

**The filtered arm's spread is a property of the queries, not of a busy box.**
Per-query p50 there runs 6.0–59.5 ms while the *round*-level median is flat at
15–19 ms across all 26 rounds of both runs — some queries simply need far more
traversal to find ten Fantasy titles. The unfiltered arm's round medians span
4.2–5.9 ms. This host's postgres was **not** quiet (the shipped worker's own
gauge refreshes plus other agents' harnesses), which is exactly why the
interleaving and the round-level control are here.

🔴 **The 8-query non-monotonicity did not reproduce, under four readings, and
the harness is where to look.** Recall@10 is **non-decreasing in `ef_search`
for every one of 12 queries** on (1) the semantic lane unfiltered, (2) the
semantic lane filtered, (3) the *first ten rows emitted* by a LIMIT-60 scan —
the truncation reading, which is the one `relaxed_order` could plausibly break
— and (4) the **fused** answer scored against the exact vector top 10, which is
what a harness driving `usher search` without `--mode semantic` measures. Zero
non-monotone pairs in any of the four. The measurement is also **exactly
deterministic**: 216 (arm, condition, query) cells × 26 repetitions across two
independent runs produced byte-identical id lists, gold included, and
`SHOW hnsw.ef_search` read back inside each scored transaction returned the
value set. Two candidate harness faults were eliminated by measurement rather
than by argument — the endpoint returns **bit-identical** vectors across 5
calls and across batch composition, and no `title_embeddings` row was written
during the run. What is left as the likely cause is the measured object: the
CLI and the API return `SearchService._rank`'s **blended** order, not the
lane's, and the fused mode dilutes the whole ef effect (a query capped at 5 of
10 there reaches 10 of 10 on the semantic lane).

**`relaxed_order` and `LIMIT` do interact, the boundary is exactly
`ef_search`, and the consequence is not recall.** The scan emits in exact
distance order while the row count asked for is ≤ `ef_search`, and breaks the
moment it passes it — at `ef_search = 100`: 10/50 rows sorted on 12 of 12
queries, 100 rows unsorted on 1, **200 rows unsorted on 12 of 12 with a row
displaced by up to 96 positions**, 1000 rows by up to 885. At 200 the same
break moves to 250 rows. The planner does not repair it: it treats the index's
order as a presorted key and puts an **Incremental Sort** on top, which sorts
only within equal-distance groups. So `_SEMANTIC` is always inside the exact
region (its LIMIT is the caller's, capped at `search_result_limit` = 50) and
**`_FUSED`'s lanes are not** — `limit * _LANE_MULTIPLIER` is 250 at that cap,
so the vector lane truncates an approximately ordered stream and *which* 250
candidates reach RRF is approximate. The `row_number()` window's own `ORDER BY`
re-sorts what survives, so the ranks are right for the set that arrived.
**Found, not fixed**, and no non-monotonicity in recall@10 follows from it.

🔴 **Over-fetch-and-re-rank buys nothing unfiltered, and the two controls say
why before the recall table does.** The distances HNSW returns are
**bit-identical** to the exact scan's for the same rows (max |delta| exactly
0.0 over 60 rows) — pgvector approximates *which* vectors it visits, never the
distance to one — so "re-score the candidates exactly" is arithmetically a
no-op, and the re-sort is a second no-op wherever the emission was already
ordered. Measured at the shipped `ef_search = 100`, fetching `10 × N` and
cutting back to 10:

| N | recall@10 unfiltered | p50 | recall@10 filtered | p50 |
|---|---|---|---|---|
| 1 (no over-fetch) | 0.858 | 4.77 ms | 0.783 | 18.15 ms |
| 2 | 0.858 | 6.14 ms | 0.858 | 26.05 ms |
| 5 | 0.858 | 5.71 ms | **0.892** | 37.66 ms |
| 10 | 0.858 | 6.08 ms | 0.892 | 38.75 ms |
| 20 | 0.858 | 5.88 ms | 0.892 | 38.35 ms |

Unfiltered it is **identical per query at every factor** — the deeper rows the
iterative scan yields are all farther than the tenth already found. Filtered it
is worth **+10.9 points**, more than `ef_search` 200 buys there (+2.5), because
under a predicate the extra rows force real traversal. So the issue's argument
that over-fetch "makes recall a property of the over-fetch factor rather than
of graph traversal" is **false on the unfiltered path and true on the filtered
one**, and it is not built: it loses to `ef_search = 200` on the unfiltered arm
(0.858 against 0.917) at a similar p50, and it is a second statement shape.
Whoever takes the filtered path should start here.

⚠️ **The issue's own single case had already decayed when this ran, and that
is the argument for the sweep rather than a quibble.** *Harry Potter and the
Philosopher's Stone* is **not** absent from the shipped lane's top 60 today: at
`ef_search = 100` it is returned at rank **8**, and the row the lane loses is
*Help! I'm a Boy (2002)*. Its exact rank moved 8 → **9** because a title named
*Harry Potter (2026)* was embedded at **06:35Z that same morning**, 1,689 rows
having landed in the previous three hours. Everything else in the issue's table
reproduces — at 200 the top 12 matches the exact scan row for row. **A default
argued from one film would have been argued from a rank that changed overnight**;
the twelve-query curve is what it rests on instead.

**And an exact scan of this table needs its parallelism turned off in a
container.** `usher-postgres-1` has the Docker default 64 MB `/dev/shm`, and a
Parallel Hash over 132,409 × 1024-lane vectors exhausts it —
`DiskFullError: could not resize shared memory segment ... No space left on
device`, mid-run, on the *gold* half of a measurement. `SET LOCAL
max_parallel_workers_per_gather = 0` returns identical rows. `nearest_for` does
not set it and has never hit this, because its own statement is a nested-loop
over seeds rather than a hash join to `titles`.

**Not measured, and named rather than implied:** real typed queries
(`search_queries`, still synthetic); any filter other than one 4.8% genre
overlap; whether the fused lane's approximate truncation is visible to a user;
whether 200 still clears its bar as the embedded population grows past 132k;
and everything the issue already lists — document length, neighbourhood
density, `m = 16` at this width.

## Weight class D and segment 6 both carry two spellings of one concept

`titles.genres` unions IMDb's vocabulary and TMDb's, and no title carries both
spellings of any concept — 20,051 `Sci-Fi` against 6,223 `Science Fiction`,
zero overlap on 1,272,866 rows (2026-08-19).
[ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md)
fixed `/browse`'s filter and facets at **read** time and deliberately left both
of this subsystem's genre readers split:

- **`search_document` weight class D.** The two spellings share no lexemes:
  `to_tsvector('english','Sci-Fi')` is `'fi':3 'sci':2 'sci-fi':1` against
  `'fiction':2 'scienc':1`. A query reaching the genres segment matches one
  half of the catalog.
- **`compose_document` segment 6 of 7**, so every stored vector carries
  whichever spelling its tier had.

**The reason it was left is this file's own arithmetic, not squeamishness.**
Normalising the column changes segment 6, so `_FINGERPRINT_SQL` correctly marks
every affected title stale and `usher index --backfill` re-embeds it —
**~1.8 h** plus a **3.3 h** `usher similar --rebuild` on this catalog by the
2026-08-13 run. That is the fingerprint working, and it is a bill to schedule
alongside any other change that stales the same documents rather than pay
twice. The enrichment-side fix that *did* ship costs nothing extra here: a
title reaching that merge already had an `INDEX` job enqueued.

Unmeasured, and sampleable with no user traffic: how many `Sci-Fi` titles
change position in a `/search` for a science-fiction query if they carry the
other label.

## RRF's absent-lane `COALESCE` costs the typed title its own top row, and the bar written to refute it CONFIRMED it (2026-08-19, issue #21)

**Issue #21 is CONFIRMED and not refuted, by a bar written to be capable of
refuting it.** The bar is `/var/tmp/usher-i21-bar/BAR.md`,
`sha256 0687983a9ec4d41f275c7b6b273b29d734ab44e5eef51f269654631bf348bc62`,
written 2026-08-19T01:52:51-05:00 — **before the first query was issued** — and
its digest was re-read at run time and matched. Harness
`scripts/measure_fusion_coverage_bias.py`; frozen results
`/var/tmp/usher-i21-bar/run-full.json`
(`sha256 e4323414ca5fd3bc8c1c317af75ca8b4a189a2726e6f733c366d35939414a3e4`).
Live catalog: **1,272,866 titles, 132,409 `title_embeddings`**, alembic `m09f`,
`openai:BAAI/bge-m3`, shipped `rrf_k = 60`, `limit = 20`, `lane_limit = 100`.
`skeleton` and *has no `title_embeddings` row* were **verified to coincide
exactly** (0 skeletons carry a vector) rather than assumed, so the frame is the
population the issue names.

**The sample.** Exact-name known-item queries, deterministic draw
(`ORDER BY md5(id::text || '20260819-i21')`). **Stratum A, n = 1,000**, uniform
over the 1,140,433-row skeleton frame — the honest population and the one the
bars score. **Stratum B, n = 300**, uniform over the 98,467 skeletons whose
`lower(name)` is also borne by an embedded title — the adversarial stratum. Two
arms per query on one query vector: the **index arm** (`PostgresSearchIndex`
alone, isolating `_FUSED`) and the **service arm** (through
`SearchService._blend`, i.e. what a viewer sees). Denominators are stated on
every number; **999 of 1,000 stratum-A names produce a non-empty
`websearch_to_tsquery`**, so the answerable subset moves nothing.

| stratum A, n = 1,000 | recall@1 `full_text` | recall@1 `fused` | delta |
|---|---|---|---|
| index arm (`_FUSED` alone) | **72.2%** | **20.1%** | **−52.1 points** |
| service arm (through `_blend`) | **71.8%** | **54.8%** | **−17.0 points** |
| index arm, *name*-level rank 1 | 79.4% | 26.5% | −52.9 points |
| service arm, *name*-level rank 1 | 78.5% | 61.9% | −16.6 points |

**Bar 1 fails by 51 points against a 1-point tolerance. Bar 2 fails at
`p = 1.5e-157`** (index) and `2.9e-35` (service), and the discordant pairs are
one-sided to a degree no sampling story survives: **521 queries lose a rank-1
`full_text` hit to an *embedded* row under `fused`, 0 lose one to another
skeleton, and 0 queries go the other way** — in 1,000 tries the semantic lane
never once rescued a query the lexical lane got wrong. Bar 3 fails too (stratum
B, −5.0 points index / −7.0 service, `p = 3.1e-05` / `1.0e-05`). **The power
control is satisfied and it is what licenses reading a null as a refutation had
one appeared**: all 300 stratum-B fused results contain at least one embedded
title, against a floor of 100.

**The displacement is the issue's own arithmetic, to the row. 521 of 521
index-arm displacements had the typed title as the *uncapped* #1 lexical
match** — the `1/61` ceiling in the issue's table, exactly — and **88.5% landed
it at fused rank 2**, one place below the enriched near-match. `Grandma's Tea`
→ *Grandma's Boy*; `The 17 Year Feast` → *17 Again*; `Choinka strachu` →
*Curse of Chucky*; `Dendy Memories` → *Dear Wendy*; `Jallad No. 1` → *Jallaad*.
The predicted failure — *"a viewer types an obscure title exactly, and an
enriched near-match takes the top row"* — is the observed one.

**Miss split, in the recorded idiom** (below the floor / truncated / dropped /
out-ranked, against the shipped suggest row's `82.8 / 0.0 / 0.0 / 17.2`). The
search path's analogues are stage for stage: *below the floor* = no
`search_document @@ websearch_to_tsquery` match, so the row is in no lane at all
(a skeleton has no vector, so the lexical predicate is its only candidacy);
*truncated* = matched, but its uncapped lexical rank exceeds the mode's cap
(20 for `full_text`, `lane_limit = 100` for `fused`); *dropped* = inside the cap
and absent from the answer, i.e. **the fusion lost it**; *out-ranked* =
returned, below rank 1.

| stratum A, service arm | below the floor | truncated | dropped | out-ranked | misses |
|---|---|---|---|---|---|
| `full_text` | 8.2 | 28.4 | **0.0** | 63.5 | 282 |
| `fused` | 5.1 | 9.7 | **14.8** | 70.4 | 452 |

**`dropped` is 0.0 for `full_text` structurally and 14.8% for `fused`, and that
column is the finding.** Every one of those is a title the lexical lane had
inside its 100-row window and the fusion pushed out of a 20-row answer. It is
the first non-zero `dropped` this file has ever recorded — the suggest path's
`levenshtein` re-rank scores 0.0% in *every* configuration measured. **The
competitor stratification is equally one-sided**: of 452 service-arm fused
misses, **402 were out-ranked by an embedded row and 50 by a skeleton**; the
`full_text` arm's 282 misses split 68 / 192 the other way.

**`coverage_t` is ~0.10 and the issue's null holds — which makes the bonus
worse, not better.** `semantic_coverage`, which the CLI prints, **is not this
quantity**: `_COVERAGE` counts `embedded / total` over
`enrichment_state <> 'skeleton'`, the enriched tier's own embedding
completeness, and it reads **1.000** on every query here while the number that
matters is a tenth of that. Four estimators, each with its denominator:
**uniform 132,409/1,272,866 = 0.1040**; **`vote_count`-weighted
26,382,667/246,921,814 = 0.1068** (a named *proxy* for demand — `search_queries`
has no typed workload to average over); **exact-name relevant sets over stratum
A, pooled 216/2,359 = 0.0916**, or 216/1,359 = 0.1589 with the drawn target
excluded from its own relevant set, and **791 of 1,000 queries have no relevant
document in the enriched tier at all**; and of the embedded relevant documents
that exist, the lexical lane already finds **44.9% (97/216)** in its own top 20.

⚠️ **REFUTED, and it is the issue's own supporting claim: enrichment on this
catalog is not a popularity proxy — it is anti-correlated with votes.** #21
argues the bias is *"a second popularity bias, stacked on the declared one"*
because the enriched tier was selected by vote count. Measured: embedded titles
average **199** votes against **541** for the rest, median **15** against
**29**, and by vote decile the coverage curve runs **23.9% in the top decile,
14.2% in the middle, 72.3% in the bottom**. So lane membership is not a proxy
for anything a viewer wants; it is an arbitrary tenth of the catalog carrying a
rank bonus. That makes the defect *worse* than #21 states it, not milder.

**The trade, both signs, because the effect has both.** *Not pre-registered* —
stratum C was drawn after the verdict above was computed and frozen, enters no
bar, and exists because half a trade is not a number. **Stratum C, n = 300,
drawn from the embedded tier**: `fused` **beats** `full_text` 68.3% against
59.0% (service arm), +9.3 points, 49 queries gained against 21 lost. The same
`COALESCE` that costs 17 points when the typed title is a skeleton buys 9 when
it is enriched — and lane membership carries no information about which case a
query is. At the catalog's own 89.6/10.4 composition that is
`0.896 × −17.0 + 0.104 × +9.3` = **−14.3 points net** on known-item lookup.
This is also why a single spot check proves nothing about the sign: `The Matrix`
under `fused` correctly promotes the 1999 film over three 2018 video essays,
and it is the 10.4% case.

⚠️ **The fused ordering is limit-dependent, and a wider request can give a worse
first row.** `lane_limit = limit * _LANE_MULTIPLIER`, so the semantic candidate
pool grows with the request. `usher search "The Lost Pass" --mode fused` returns
the typed title at **rank 1 at `--limit 3` and rank 2 at `--limit 20`**, behind
*The Forward Pass* (1929). Any spot check of this defect must state its limit;
`--limit 3` hides it.

**Two controls, because a harness that agrees with nothing is not evidence.**
(1) The shipped CLI reproduces the service arm at the same limit — `Choinka
strachu` and `Jallad No. 1` are displaced through `python -m usher search` at
`--limit 20` exactly as recorded. (2) **The harness wrote nothing, proven by
arithmetic rather than asserted**: `search_queries` went 47 → 67 across the
whole session and 20 is exactly the number of *CLI control* invocations, so the
3,200 index-and-service searches the harness itself issued (it passes
`user_id=None`) left zero rows.

**Not settled by this run, named rather than implied.** Only exact-name
known-item queries were scored — mood, discovery and partial-title queries are
untouched, and the semantic lane is likelier to earn its keep there. Only
recall@1; nDCG and recall@5 are unmeasured. `hnsw.iterative_scan`'s interaction
with the bias is unmeasured, so the semantic lane's row count remains
configuration-dependent. And **no fix is measured here**: convex fusion is
#21's proposal, it replaces a parameter-free rule with one that must be tuned,
and this run establishes only that the change is warranted — not what its
weights should be.

## Issue #25's exact-name key closes most of #21, and the residual is the tie it cannot break (2026-08-19, issues #21 + #25 re-run)

**The same bar, re-run unchanged against merged code.** Same harness
(`scripts/measure_fusion_coverage_bias.py`), same pre-registration
(`/var/tmp/usher-i21-bar/BAR.md`, digest re-read at run time and matched:
`0687983a9ec4d41f275c7b6b273b29d734ab44e5eef51f269654631bf348bc62`), same seed
`20260819-i21`, same strata, same denominators, same thresholds. Nothing was
edited — the only difference is the code under it. Results
`/var/tmp/i21-rerun/post25-ef100-full.json` (and `-ef200-` for the shipped
`ef_search`).

**That the run used merged code is proven, not asserted.** A read-only
`before_cursor_execute` listener recorded every statement the run sent to
PostgreSQL: **3,200 of 3,200 fused statements and 3,200 of 3,200 `full_text`
statements carried `lower(t.name) = lower(btrim(:query))` together with
`ORDER BY exact_name DESC`** (3,200 = 1,600 queries x 2 arms). `main` contains
zero occurrences of `_EXACT_NAME` and its `_FUSED` outer sort is
`ORDER BY score DESC, id`, so the two branches are distinguishable from the SQL
alone and the run is on the right side of the difference.

| stratum A, n = 1,000 | `full_text` | `fused` | delta |
|---|---|---|---|
| index arm, pre-#25 | 72.2% | 20.1% | **-52.1** |
| index arm, post-#25 | 82.5% | 81.6% | **-0.9** |
| service arm, pre-#25 | 71.8% | 54.8% | **-17.0** |
| service arm, post-#25 | 83.4% | 81.6% | **-1.8** |

**The mechanism #21 named is gone.** Discordant pairs where an *embedded* row
took rank 1 from the typed skeleton fell **521 -> 9** on the index arm (98.3%)
and **172 -> 10** on the service arm (94.2%). The `dropped` bucket — #21's
first-ever non-zero, and the one an `ORDER BY` was not obviously able to rescue
— went **8.4% -> 0.0%** (index) and **14.8% -> 0.0%** (service), **67 rows ->
0**. It was rescuable after all: the key sorts inside the lexical CTE ahead of
the lane's own `LIMIT`, so the row is no longer truncated out of the lane
before fusion sees it.

**Read the miss split in absolute rows, not percentages.** Stratum-A fused
misses fell 799 -> 184 (index), so every surviving bucket's *share* rose while
its count did not: `below the floor` 23 -> 23 rows, `truncated` 44 -> 31,
`out-ranked` 665 -> 130, `dropped` 67 -> 0. A split whose denominator moved by
4.3x cannot be compared bucket-share to bucket-share.

⚠️ **Stratum B got worse, and that is the honest residual.** On the 300
skeletons whose name is *also* borne by an embedded title, the delta widened
**-5.0 -> -8.7** (index) and **-7.0 -> -14.3** (service) — both arms improved
in absolute terms (service `full_text` 7.7% -> 18.0%, `fused` 0.7% -> 3.7%) but
`full_text` improved more. The reason is structural: when the competitor is
*also* an exact name match, `exact_name DESC` **ties**, the sort falls through
to `score DESC`, and there the enriched row's two-lane sum wins exactly as #21
described. **289 of 289 stratum-B fused misses are to a same-name row.** #25's
key breaks the ordering only when the competitor is *not* an exact match; it
cannot break a tie it creates.

**The catalog-composition net, recomputed.** `(1 - t) x delta_A + t x delta_C`
at `t = 0.104`, with stratum C (n = 300, drawn from the embedded tier, not
pre-registered) supplying the other side of the trade:

| | delta A | delta C | net |
|---|---|---|---|
| service arm, pre-#25 | -17.0 | +9.3 | **-14.3 pts** |
| service arm, post-#25 | -1.8 | +11.0 | **-0.47 pts**, 95% CI [-1.96, +1.02] |
| index arm, post-#25 | -0.9 | +7.7 | **-0.01 pts**, 95% CI [-0.63, +0.61] |

**Both nets straddle zero.** Fused went from decisively net-negative on this
catalog to statistically indistinguishable from `full_text`, while winning
outright on the 10.4% of the catalog that carries a vector (service arm stratum
C **75.7% -> 86.7%**, +11.0).

**`ef_search` is not a confound.** The pre-#25 run recorded 100; the shipped
default is now 200 (issue #32). Re-running the whole bar at both gives nets of
-0.47 (100) and -0.49 (200) on the service arm — the comparison survives the
parameter change.

**The bar's own verdict is still CONFIRMED, and the arithmetic says why.**
B1 now *passes* on the index arm (-0.9 is inside the 1.0-point tolerance) and
B2 now *passes* on the service arm (`p = 0.995` — by the issue's own accounting,
which excludes misses to another skeleton, fused wins 24 and loses 10). B3
fails on both arms, which is the stratum-B tie above. So the verdict is carried
entirely by the adversarial stratum: **#21 is narrowed to same-name
collisions, not closed.**

**The harness still wrote nothing.** `search_queries` 67 -> 78 across the
session, and all 11 rows are `The Matrix`/`space opera` from a *different*
agent's live-container work on issue #31 — none is a skeleton name from either
draw. The 6,400 searches this run issued (it passes `user_id=None`) left zero
rows, and the live catalog was read-only throughout.

**Not settled by this run.** Still only exact-name known-item queries and still
only recall@1 — mood and partial-title queries, where the semantic lane should
earn most, remain unmeasured. And the stratum-B tie has no measured fix: the
obvious one (break the `exact_name` tie on popularity rather than on the fused
score) is a proposal, not a result.
