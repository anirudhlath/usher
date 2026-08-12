# ADR-0035 — The MovieLens user-tag term is not built, and the zero is the reason rather than the rate

**Status:** Accepted — **a recorded refusal, which is the outcome this gate was
built to be able to produce.** The gate ran 2026-08-12 (M9 tasks S1–S5) against
a real enriched catalog and the term is not built in M9. Records the outcome
for [05](../05-search-and-similarity.md)'s `### Similarity` section and names
one scoped follow-up with its number attached. **Does not settle the tag
*genome*'s weight** — that is a different question about a different signal and
it belongs to [ADR-0024](0024-the-genome-is-one-dense-vector-per-title.md).

**No line of `src/` changed on this arm.** The pre-registered bar named
`services/similar.py` and `tests/unit/test_services_similar.py` as files that
must not appear in the diff below 10%, and they do not.

## Context

`SimilarityService` blends four signals and the fourth is the MovieLens tag
**genome** — one dense `halfvec(1128)` per title, 16,376 movies,
[ADR-0024](0024-the-genome-is-one-dense-vector-per-title.md). M7 shipped it at
weight 0.25 on a measured candidate-pair rate of **1.81%** — 9,069 of 502,000
pairs over **5,020** owned, name-selected, pre-TMDb seeds, which is a population
and not a baseline — against a **10% floor** that a 0.25 weight assumes, and
wrote the question down rather than answering it.

The obvious cheaper signal sits in the same archive and this project has never
read it. `ml-latest/tags.csv` is **21,274,899 rows / 85 MB** of *free-text user
tags* — a different artefact from the genome, covering far more movies, and
`adapters/bulk/movielens.py:27-39` reads three of the archive's seven members
and not that one. So the question M9 asks is not "is the genome worth its
weight" but **"is there a second, wider tag signal that clears the floor the
genome misses?"**

**The call is contested because the coverage arithmetic points the right way.**
MovieLens tags reach **49,055** titles in this catalog against the genome's
15,565 — three times as many — and a reasonable person reading those two
numbers builds the term. A pre-registered bar (`/tmp/m9-gate/BAR.md`, written
before any measurement, revised once before any measurement after a critic
found the first version's bands inverted) fixed **one** threshold so the answer
could not be negotiated afterwards:

| pair rate | verdict |
|---|---|
| **≥ 10%** | Build in M9. |
| **< 10%** | Do not build. This ADR records the number and the decision. |

The rate that decides it is the **candidate-pair rate**: of all (seed,
candidate) pairs a real neighbour rebuild considers, the fraction where **both
sides** carry the signal. Single-side coverage does not decide it and never
did — a pair needs two sides, so coverage enters roughly squared.

## Decision

**1. The user-tag term is not built in M9.** No `title_tags` table, no
`tags.csv` importer, no `NeighborCandidate` field, no `_WEIGHTS` entry, no
migration id requested. The measured pair rate is **6.0821%**, which is the
`< 10%` arm read off the bar without adjustment.

**2. The refusal rests on two independent reasons, and the second is the
stronger one.** The rate misses the floor by 39%. But the *instrument* is
wrong for this data in a way no coverage growth repairs: **`_jaccard` answers
`None` only for an empty set**, so two titles that each carry five tags and
share none yield a hard **`0.0`**, which `_blend` renormalises as a confident
negative. On the marginal population — tagged, no genome — the **median pool
pair shares no tag at all and 62.3% of them share none**. A term added to
promote related films would **demote most of the pairs it fired on**, relative
to pairs carrying no tag data at all.

**3. So the rate is explicitly *not* the thing a future reader should re-check
first.** Measured single-side coverage at `>= 1 tag` on the same population is
**34.47%**, and a pair rate projected from it clears 10% on both the
independent-draw arithmetic (0.3447² = **11.9%**) and the correlated one
(× the measured 1.37 factor = **16.3%**). **Lowering the threshold buys the
rate and makes the zero worse** — at one to four tags a disjoint pair is the
overwhelming default. A gate that could be passed by moving a threshold is not
what should decide this, which is why this decision is written around the
distribution rather than around the rate.

**4. One scoped follow-up is named, unassigned, and it is a measurement rather
than a build.** Below. Nothing in `docs/prd/09-roadmap.md` is claimed by this
decision.

## Consequences

**Gained.** The question is closed with a number and a distribution instead of
a hunch, over a genuinely enriched population, at a cost of zero production
code and zero schema. The next reader inherits evidence: the rate, its
denominator, its sample, the shape of the underlying overlap, and the specific
arithmetic that would have to change for the answer to change.

**Given up.** A signal that really is present — tag-set Jaccard on the marginal
population is **7.7× chance**, not near-chance — is left on the floor for this
milestone. That cost is stated rather than argued away: this is a refusal to
*build now*, not a finding that user tags are noise.

**What the build would have cost, priced before the answer was chosen rather
than after.** An importer for a member this project has never read
(21,274,899 rows / 85 MB), a `title_tags` table, a **migration id that does not
exist** (`m09a` is M1's, `m09b` is group T's, `m09c` is spare and must be
*requested*), a `NeighborCandidate` field, two widened statements in
`db/repositories/search.py`, both fakes, the contract suite, and a full
`usher similar --rebuild` because adding a term re-weights the other four and
moves every stored score. None of that is proportional to a signal that fails
its own floor.

**Not closed, and named so nobody reads this as broader than it is.** Nothing
here measures whether a tags term makes a neighbour list *better*. A pair rate
is a statement about **membership**; relevance would need judgements this
project has never had. The same caveat M7 attached to the genome's 0.25 weight
applies unchanged.

**The genome's weight is not settled here.** S5 re-measured it at **2.4746%**
over the same pool — a *second measurement*, not a delta against 1.81%, because
[S1 established](../09-roadmap.md) that M7's number came from 5,020
name-selected seeds in a database that no longer exists. It is still four times
below the floor. What follows from that is ADR-0024's to say.

## The follow-up, with the number attached

**Measure a tag term whose zero is not a verdict, before anyone proposes one
again.** Three read-only measurements, no table, no importer, no migration:

1. **The pair rate at `>= 1 tag`.** Projected at **11.9%–16.3%** from a
   measured 34.47% single-side coverage; if it lands there, the *rate* arm of
   this refusal is discharged and only the distribution arm remains — which is
   the point.
2. **The share of firing pairs whose overlap is empty, at whatever threshold
   clears.** **62.3%** at `>= 5` on the marginal population, and it can only
   rise as the threshold falls. A term is proposable only if this is small, or
   if the instrument in (3) makes it irrelevant.
3. **Whether a different instrument over the same rows puts the median firing
   pair above zero** — TF-IDF cosine over the tag strings, or a dense embedding
   of the joined tag text. These are three different signals with three
   different failure modes (unnormalised free text: `sci-fi` / `scifi` /
   `science fiction`, typos, personal notes), and picking one by default would
   be the decision made without the argument.

**Waiting for more enrichment is explicitly not the follow-up, and this is the
number that says so.** At the measured 1.37× correlation factor, clearing 10%
at the `>= 5` threshold needs **27.0%** single-side coverage against the
21.094% measured — about **7,700 more `>= 5`-tagged titles inside the same
130,647-title population**. The archive is **frozen at 2023-07-20 and
movies-only**, and **19,222** of this catalog's tagged titles carry *one to
four* tags and no genome, so they cannot enter that population at all. Every
further enrichment pass reaches titles MovieLens never tagged, which grows the
denominator and moves coverage **down**. The ceiling is a property of the
dataset, not of this project's progress.

## Evidence

**One walk, both signals, one pool, 2026-08-12 (task S5).**
`scripts/measure_pair_rates.py` drove `SimilarityService.rebuild()`'s own page
shape read-only over the whole embedded population: **130,647 seeds, 262 pages
of 500, 13,064,700 candidate pairs, 5,125 s (85.4 min)**, on a box the harness
verified idle, counted over the **pool** `nearest_for` returns and never over
stored rows, writing nothing.

| both sides carry | pairs | **pair rate** | seeds | single-side | coverage² | measured ÷ coverage² |
|---|---|---|---|---|---|---|
| a `genome_scores` row | 323,297 | **2.4746%** | 15,525 | 11.883% | 1.412% | 1.75× |
| **≥ 5 MovieLens tags** | **794,606** | **6.0821%** | 27,558 | 21.094% | 4.449% | 1.37× |
| ≥ 10 MovieLens tags | 404,993 | **3.0999%** | 18,470 | 14.137% | 1.999% | 1.55× |

**Counting the pool rather than the stored rows is the whole method, and the
wrong spelling was written first on purpose.** An accumulator over stored rows
answers **147/1,000 = 14.70%** where the pool answers **182/1,560 = 11.67%** on
the same 40-title fixture — plausible, four points high, and high **by
construction**, because the stored rows are the pool already sorted by a blend
weighting the very signal being counted at 0.25. The case was red against that
spelling before it was fixed (`assert 1000 == 1560`), and the walk's genome
counter is byte-for-byte `rebuild`'s own `pairs_with_tags / candidate_pairs`,
pinned by `tests/unit/test_scripts_measure_pair_rates.py` over one shared fake.

**The Jaccard distribution, which is what decides this.** Measured against
`ml-latest/tags.csv` itself (21.3M rows, read outside the tree, **no row
committed**) over a uniform random 2,000-seed sample, whose own `>= 5`
both-sides rate of **6.4125%** agrees with the exhaustive walk's 6.0821% within
5.4% and is therefore the sample's control:

| tag-set Jaccard | n | mean | median | p90 | p99 | share sharing **no** tag |
|---|---|---|---|---|---|---|
| pool pairs, both `>= 5` | 12,825 | 0.0221 | 0.0055 | 0.0625 | 0.1765 | **47.3%** |
| random pairs, same population | 20,000 | 0.0038 | 0.0000 | 0.0115 | 0.0625 | 83.3% |
| pool pairs, **marginal** (tagged, no genome) | 2,595 | 0.0261 | **0.0000** | 0.0833 | 0.2222 | **62.3%** |
| random pairs, marginal | 20,000 | 0.0034 | 0.0000 | 0.0000 | 0.0769 | 92.7% |

**This is [ADR-0014](0014-absence-is-not-zero.md) arriving from the other
side, and the distinction it did not have to draw before.** ADR-0014 says an
*absent* signal must be `None` rather than 0.0, and `_jaccard` honours it. What
this table shows is a case where **presence with no overlap** is also not
evidence: with a closed ~19-value genre vocabulary, disjointness means
something; with an open long-tail user-tag vocabulary at a median of four tags
a title, disjointness is the *default*. [05](../05-search-and-similarity.md)
already argues from vocabulary size that genres and keywords must be two terms
rather than one — the same argument, applied to user tags, lands on refusing
the term rather than on splitting it.

**Three of the bar's four guesses were refuted, and the one that survived is
the one the bar turns on.**

- **"3–5%" — refuted upward at 6.0821%, and its arithmetic failed on both
  inputs.** It scaled M7's single-side-to-pair ratio of 0.238 by a *tier-wide*
  14.46%; the population actually embedded carries **21.094%**, and **the ratio
  is not constant even inside one walk** — 2.4746/11.883 = **0.208** for the
  genome against 6.0821/21.094 = **0.288** for tags, a 38% spread over the
  identical pool.
- **"It does not clear 10%" — confirmed.** The only survivor.
- **"`>= 10` tags scores a higher rate on fewer titles" — refuted, and
  backwards.** It scores **3.0999%**, and it *cannot* score higher: pairs at
  `>= 10` are a strict subset of pairs at `>= 5` over an identical denominator,
  so the rate is **monotone by construction**. The gap decomposes exactly:
  coverage falls 21.094% → 14.137% (×0.670), both sides squares it (×0.449),
  and the heavily-tagged population genuinely *is* more clustered
  (1.55/1.37 = ×1.135). 6.0821% × 0.449 × 1.135 = **3.099%**, the measured
  value. More tags per title is a real effect, larger than the `>= 5`
  population's, and it is swamped by its own coverage loss because both-sides
  squares everything.
- **"Jaccard on the marginal population is near-chance" — refuted on its
  mechanism at 7.7× chance (0.0261 against 0.0034), and its conclusion
  confirmed anyway, for the sharper reason in Decision 2.** The bar's
  illustrative *"two 4-tag sets sharing one tag gives 0.14"* sits between the
  marginal distribution's p90 and p99, not at its centre.

**`coverage²` is wrong in a measurable direction, and this project now has the
correction factor rather than the warning.** All three signals beat their
independent-draw prediction by **1.37–1.75×**: pool membership and signal
membership are positively correlated, most strongly for the genome, whose
coverage concentrates in popular, older, heavily-embedded films. Every
projection above states which factor it used.

**`nearest_for` was asserted deterministic rather than assumed**, over a full
500-seed page: two reads returned identical pools, id for id and in order.
That is what licenses comparing this walk with a later
`SimilarityService.rebuild()`.

Every measurement above, with its denominators and its reconciliations, is in
`.claude/rules/rows-and-genome.md`.

## Uncertainty

Named rather than implied.

- **The input is very slightly generous about its own threshold.**
  `ml_tags_tmp.n_tags` counts tag *applications*, not distinct tags: of the
  27,558 embedded titles it puts at `>= 5`, **61.7% disagree with a case-folded
  distinct count** and **485 (1.8%)** hold fewer than five distinct tags. At
  1.8% it moves no verdict; a threshold named "≥ 5 tags" is nonetheless
  counting something slightly wider than its name, and a follow-up measuring
  `>= 1` should count distinct tags from the start.
- **The tag plant is not stationary across an enrichment run**, so this
  measurement does not reproduce to the digit. Re-measured after S3's crawl,
  the join gives 49,055 / 15,385 / 33,670 / 14,448 / 6,266 against the bar's
  pre-run 49,05**6** / 15,385 / 33,67**1** / 14,448 / 6,266 — exactly one title
  stopped matching. The mechanism was demonstrated rather than assumed:
  **`imdb_id` is in `EnrichService._ENRICHABLE`**, so TMDb's
  `external_ids.imdb_id` overwrites IMDb's own, and **28 enriched titles now
  carry an `imdb_id` that is not a tconst IMDb holds at all** (a lower bound —
  a rewrite onto another valid tconst is invisible to that check).
- **"Tagged movies" is the wrong label on a right number.** Joined over titles
  of any kind the figure is 49,055; filtered to `kind = 'movie'` it is 48,674,
  and the difference is **381 titles this catalog classifies as `series` whose
  IMDb ids appear in a movies-only dataset**. They cost the walk nothing — the
  embedded population holds zero series.
- **The `>= 1` projection is a projection.** 11.9%–16.3% is arithmetic over a
  measured 34.47% single-side coverage, not a walk. The correlation factor is
  likely *lower* for a broader population than for `>= 5`, so 16.3% is the
  optimistic end and 11.9% is the independent-draw floor.
