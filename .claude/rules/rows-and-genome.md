---
paths:
  - "src/usher/services/rows/**"
  - "src/usher/services/home.py"
  - "src/usher/services/taste.py"
  - "src/usher/services/derive.py"
---

# The home screen, row providers and the tag genome

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**M7's measurements, taken 2026-08-04/05 on this host against
`pgvector/pgvector:pg17` (pgvector 0.8.6) unless stated otherwise. The
`titles.popularity` re-measure has its own entry inside M6's gate section
below, because it is a correction to that gate's headline.**
**The genome cosine does NOT saturate, measured against a bar written before
the run — so the vectors ship raw and are not mean-centred.** Over all 16,376
vectors and all **268,157,000** ordered off-diagonal pairs: **mean 0.6101, sd
0.0913, min 0.2556, p1 0.4075, p50 0.6095, p99 0.8165, max 0.9913**, top-10
neighbour gap **0.2456**. The bar, written first: saturated if mean ≥ 0.70, or
p1 ≥ 0.50, or sd < 0.05, or the top-10 gap < 0.15. **No clause fired**, and it
is measurably better-ordered than a signal this repository already shipped
(name-only skeleton embeddings, mean 0.5867 / sd 0.055). Both centred variants
were measured and neither ships — per-vector `v − mean(v)` gives 0.3875 /
0.1249 / gap 0.3813, per-tag `v − μ` gives 0.0034 / 0.1887 / gap 0.6313 — and
**nothing is foreclosed**, because the stored population *is* the corpus:
`SELECT avg(relevance::vector)::real[] FROM genome_scores` recovers μ (a bare
`avg(relevance)` is not usable and `halfvec` has no subscripting).
**"A full pairwise cosine over all 16,376 vectors runs in 1.190 ms" is wrong
by five orders of magnitude**, and it was repeated in five places in the M7
plan. A real self-join is 121M unordered pairs of 1,128 lanes and measures
**384 s**. The numbers that matter, on a real 15,565-row table: **`get_pair` is
0.062 ms** (two primary-key probes under a `BitmapOr`, the only read this table
has), and an **unindexed KNN — one seed against all 15,565 — is 59.4–66.2 ms**
at 93,617 buffers, dominated by one TOAST fetch per row. The no-HNSW decision
is unchanged and always rested on the access pattern; but 1.190 ms would have
foreclosed the question of whether a consumer could want KNN, and 384 s
reopens it.
**The genome's real coverage, with its denominators — measured 2026-08-05
against a `--phase all` catalog of 1,271,570 titles.** 15,565 genome vectors
joined: **1.22%** of all titles, **1.73%** of 899,991 movies, **7.61%** of the
≥100-IMDb-vote priority tier (measured at 204,494 titles, not the ~189k PRD 04
estimated), **10.68%** of a real household's 5,020 owned titles. **The number
that decides whether the term does anything is none of those** — it is the
*candidate-pair* rate, because both sides of a pair need a vector: **1.81%**
(9,069 of 502,000 pairs), **measured, never squared** (`coverage²` would have
said 1.14% and a real pool is not an independent draw). That is far below the
10% floor the weight assumes, so at 0.25 the genome reorders about one
neighbour list in fifty-five. **The term is kept anyway and the choice deferred
to M9**: 1.81% is a conservative floor (no TMDb key ran, so documents are
name-shaped and the pool is name-selected, which weakens exactly the
correlation being measured), and `blend_fingerprint` makes reverting cheap and
detectable. The genome is **movies-only and frozen at 2023-07-20**, so coverage
of anything newer is structurally zero and decays.
**The sequential row build's cost, so boundary call 8 is a decision rather than
a preference.** Measured 2026-08-04 via `usher home --repeat 5` against a real
**1,271,570**-title catalog with a synthetic household (5,200 owned copies, 360
watch states over two years including 60 episodes, 50 collections, 1,800
credits and 6,000 `title_neighbors` rows): **cold p50
23.9 ms, p95 35.9 ms**, warm 0.0 ms, 8 rows / 115 cards, slowest provider
`because-you-watched` at 4.3 ms = **34%** of build time. The revisit rule was
written before the run and needs **both** clauses: p95 > 400 ms *and* no single
provider at ≥ 50%. p95 is **11× under** the budget, so the second never
applies.

⚠️ **And that p95 is a property of the household, not of the composer.**
Re-measured 2026-08-05 against the scale ceiling — `scripts/measure_rows.py`'s
full seeding, **1,277,878 owned items and 1,277,878 watch states, 1,086,149
played** — compose is **p50 710.3 ms, p95 783.4 ms**, 2× *over* the budget,
with `genre-affinity` at **98%** of build time and `next-up` costing **302.9 ms
to propose**. The decision does not move, because the revisit rule needs *both*
clauses and the second does not fire: the answer is to fix one provider, not to
run nine on one session. **Never quote the 11× without the 5,200-copy
household it belongs to.**
[ADR-0025](../../docs/prd/decisions/0025-rows-build-sequentially.md).
**A non-overlap assertion passes against the exact `gather` it exists to
kill.** `asyncio.gather` over coroutines that never suspend produces N
*disjoint* windows, so "these did not overlap" is satisfied by the concurrency
the case forbids. What has teeth is a **depth recorder shared by the
providers** asserting `max_in_flight == 1` — `AsyncSession`'s real contract,
one statement in flight at a time — which a `gather` drives to 9 on the first
pass; and it needs its own control, because deleting the recorder's
`await asyncio.sleep(0)` makes every implementation look sequential. A second
case AST-scans `services/home.py` for `gather`/`TaskGroup`/`create_task`/`wait`,
walking `ast.Import` **and** `ast.ImportFrom` and matching the bare name as
well as the attribute.
