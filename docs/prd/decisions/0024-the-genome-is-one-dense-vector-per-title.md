# ADR-0024 — The tag genome is one dense vector per title, not a tall relevance table

**Status:** Accepted — corrects PRD 02's implied shape. **Amended 2026-08-07
by M8 Task 19:** the vector shape stands unchanged; the *vocabulary* half of
the decision ("the tag vocabulary is not stored") was taken on stated terms
that M8 has now met, and `genome_tags` ships in migration `m08b`.
**Amended again 2026-08-12 by M9 Task S7:** the shape still stands and the
table still ships — **the similarity *term* does not.** The candidate-pair
rate was re-measured over an enriched population at **2.4746%** against the
**10%** floor the 0.25 weight assumed, so `_WEIGHTS["tags"]` and the `tags=`
argument at `_neighbors_for`'s call site are removed together. The vectors,
the port field, the pair statement and `usher similar --rebuild`'s counter all
stay. See the dated amendment at the end.

## Context

The MovieLens tag genome is a relevance score for every (movie, tag) pair:
16,376 movies × 1,128 tags in `ml-latest.zip`, distributed as
`genome-scores.csv` with one row per pair. [02](../02-data-model.md) promised a
`genome_scores` table holding *"MovieLens tag-genome relevance vectors, where
available"* and nothing more, and [05](../05-search-and-similarity.md) wanted
one number out of it: the cosine between two titles' vectors, as a term in
`SimilarityService`'s blend.

**The call is contested because the source file's shape implies the answer.**
The dump is tall, the phrase "relevance scores" is plural, and a reasonable
person reading PRD 02 — or reading `genome-scores.csv` — would build
`(title_id, tag_id, relevance)` with a composite primary key. That is the
normalised shape, it is what the data physically is, and it makes a single
score addressable. It is also 47× larger and turns the similarity term into
arithmetic in Python.

## Decision

`genome_scores` is **one row per title** holding one `halfvec(1128)`:

```sql
title_id        uuid PRIMARY KEY REFERENCES titles(id) ON DELETE CASCADE
relevance       halfvec(1128) NOT NULL
genome_revision text          NOT NULL
computed_at     timestamptz   NOT NULL DEFAULT now()
```

**No HNSW index and no index at all beyond the primary key.** The access
pattern is a *pair* lookup by `title_id`, not a KNN, and an HNSW index cannot
serve a lookup by primary key.

**The tag vocabulary is not stored *by this decision*.** Nothing in M7 reads a
tag *name*: a cosine needs the two vectors and the guarantee that their
positions mean the same thing. `genome-tags.csv` is read by the importer to
verify contiguity and width, then thrown away.

**Amended 2026-08-07 (M8 Task 19), on the terms this decision set out.**
`genome_tags(tag_id, tag, genome_revision)` ships in migration `m08b`, loaded
by the same `bootstrap --phase movielens` from the member it was already
reading. Nothing about the *vector* shape
changes; what changed is that a consumer of the names arrived, exactly as the
"Also" section below predicted, and `genome_revision` is what the vocabulary's
own copy of that column is compared against.

## Consequences

**Gained:**

- **The similarity term is a single `<=>`** — the operator `SimilarityService`
  already blends for embedding cosine, so the fourth signal costs an accessor
  and a weight rather than a new code path. *(True in both directions, which is
  what made the 2026-08-12 removal two lines: the `<=>` is still computed and
  still counted, it is simply no longer weighted. See the amendment.)*
- **45 MB against 2,106 MB.** Measured on `pgvector/pgvector:pg17` (pgvector
  0.8.6) at the real dimensions, 16,376 rows: the dense form is **45 MB**
  (1,096 kB heap + 43 MB TOAST + 624 kB index), the tall form is **18,472,128
  rows and 2,106 MB** — **47×**, against a database
  [08](../08-operations.md) budgets at **8–12 GB total**. The tall form alone
  would be a fifth of the entire budget for a signal covering 1.22% of the
  catalog.
- **`genome_revision` is [ADR-0020](0020-derived-state-carries-its-fingerprint.md)'s
  shape on a table that badly needs it.** The tag vocabulary can change between
  releases, and two vectors from different releases are type-identical,
  same-width and otherwise indistinguishable — so a half-migrated table yields
  cosines that are wrong *and plausible*. `GenomeRepository.get_pair` returns
  `None` across a mismatch rather than a number.

**Given up:**

- **A single (movie, tag) relevance is no longer addressable in SQL.** Reading
  one lane means reading the whole vector. Nothing wants to, and the day
  something does, the vector is still there.
- **The vector is TOASTed, so every genome read pays a TOAST fetch.** 1,128
  halfvec lanes is 2,256 bytes plus a header, past Postgres's ~2 kB inline
  threshold: the heap holds 1,096 kB of pointers and TOAST holds 43 MB.
  Invisible at 16,376 rows and one `<=>` per candidate pair; **not invisible at
  1.27M**, which is one more reason this table's population is the genome's own
  and not the catalog's.
- **The dense form assumes the matrix stays dense.** It is dense today — every
  one of 16,376 movies carries a value for every one of 1,128 tags, verified by
  counting — but that is a property of this snapshot rather than a promise, so
  the importer verifies the run structure and fails loudly rather than
  assembling a silently short vector. See [04](../04-catalog-bootstrap.md).

**Also:**

- **M8 inherited the vocabulary, and the cost was paid exactly as recorded.**
  An LLM prompt that wants to say *"atmospheric, thought-provoking"* needs the
  words. Paying for it was a 1,128-row table plus a loader step in a phase that
  already reads the file, and one migration — and `genome_revision` is what
  made it safe rather than a deferral-by-omission. **Shipped 2026-08-07 as
  `genome_tags` / `m08b`**, with the vocabulary carrying the same revision as
  the vectors it explains and `GenomeRepository.vocabulary` refusing across a
  mismatch. The one thing this paragraph did not anticipate: the refusal is an
  *error* rather than `get_pair`'s `None`, because a mislabelled lane is prose
  on a screen rather than a wrong number — [02](../02-data-model.md) carries
  that argument.

**Rejected:**

- **The tall `(title_id, tag_id smallint, relevance real)` table** — the shape
  the source file and PRD 02's wording both imply. 2,106 MB, and it stores
  16,376 copies of a tag id to express a matrix with no holes in it.
- **`real[]`, one array per title** — 88 MB, between the two and worse than
  both: **no operator class**, so the similarity term stops being a `<=>` and
  becomes a dot product in Python over 1,128 floats per candidate pair.
- **An HNSW index on the vector.** It cannot help a lookup *by* `title_id`, and
  M6 separately measured a planner-*preferred* index costing 4.3× for
  byte-identical recall. `tests/integration/test_genome_repository.py` asserts
  the index set, so a later migration cannot quietly add one.
- **Mean-centring the vectors before storing them.** Measured, and it does not
  ship — see Evidence.

## Evidence

**Storage**, measured rather than estimated, on `pgvector/pgvector:pg17`
(PostgreSQL 17.10, pgvector 0.8.6) at the real dimensions:

| Form | Rows | Total size |
|---|---|---|
| `halfvec(1128)`, one row per title | 16,376 | **45 MB** |
| `real[]`, one row per title | 16,376 | 88 MB |
| `(title_id, tag_id smallint, relevance real)`, PK on the pair | **18,472,128** | **2,106 MB** |

**Access cost**, measured against a real 15,565-row load: `get_pair` is
**0.062 ms** — two primary-key probes under a `BitmapOr`, which is the only
read this table has. An unindexed KNN, one seed against all 15,565, is
**59.4–66.2 ms** at 93,617 buffers, dominated by one TOAST fetch per row.
Nothing asks for that today; if something ever does, the decision reopens on
evidence rather than on this ADR.

⚠️ **An earlier draft of this decision — carried in the M7 plan in five
places — claimed "a full pairwise cosine over all 16,376 vectors runs in
1.190 ms".** It does not. A real self-join is 121M unordered pairs of 1,128
lanes and measures **384 s**; 1.190 ms is about the cost of a *single pair*.
The decision is unchanged because it never rested on the scan — it rested on
the access pattern — but the corrected number matters: 1.190 ms would have
foreclosed the question of whether a consumer could ever want KNN here, and
384 s reopens it.

**The signal is not inert**, which had to be checked before the shape was worth
arguing about. Measured over all 16,376 vectors and all **268,157,000** ordered
off-diagonal pairs, against a saturation bar written down before the run
(saturated if mean ≥ 0.70, or p1 ≥ 0.50, or sd < 0.05, or the top-10 gap
< 0.15): **mean 0.6101, sd 0.0913, min 0.2556, p1 0.4075, p50 0.6095, p99
0.8165, max 0.9913**, top-10 neighbour gap **0.2456**. **No clause fired.** For
scale, it is measurably better-ordered than a signal this project already
accepted and shipped — real embeddings over name-only skeletons are mean
0.5867 / sd 0.055, recorded as *"crowded, but ordered"*.

**Both mean-centred variants were measured alongside and neither ships.**
Per-vector `v − mean(v)` gives 0.3875 / 0.1249 / gap 0.3813; per-tag `v − μ`
gives 0.0034 / 0.1887 / gap 0.6313. **Nothing is foreclosed**, because the
stored population *is* the corpus: μ is recoverable from `genome_scores` itself
(the working spelling is `SELECT avg(relevance::vector)::real[] FROM
genome_scores`, because `halfvec` does not support subscripting and a bare
`avg(relevance)` is not usable). A later milestone wanting either centring
takes it as a read-side decision with no re-import.

**The write path needed no new codec.** `pg_cast` carries `real[] → halfvec`
and asyncpg has a native `float4[]` codec, so the staging column is `real[]`
and the cast is in the `INSERT … SELECT`; nothing new touches the shared
staging path. Staging as `text` was measured as **1.7× faster** (median 25.5 ms
against 43.2 ms over 7 runs of 250 rows) despite the larger payload, because
asyncpg's array encoder walks 250 × 1,128 Python floats where a pre-built
string is one `memcpy`. Not taken — the difference is ~1.2 s across a whole
import, against a hand-rolled encoder nobody would maintain.

## Uncertainty

**The archive is frozen and movies-only, so coverage decays by construction.**
`ml-latest.zip` has not moved since 2023-07-20, and the genome covers no
television at all — so coverage of anything released since is structurally
zero and falls every year whether or not anyone touches this table. That is a
property of the signal rather than a defect to fix, and it is the reason the
term's weight is a question [05](../05-search-and-similarity.md) reopens in M9
rather than a constant.

**The measured pair rate is below the floor the weight assumes.** Both sides of
a candidate pair need a vector for the term to contribute, and that rate is
**1.81%** (9,069 of 502,000 pairs) against a 10% floor — measured, never
squared, on a catalog with no TMDb enrichment. **The population is part of the
number and the arithmetic recovers it**: 502,000 pairs over a 100-title
candidate pool is exactly **5,020 seeds**, and those 5,020 were one household's
owned titles, moved onto the enriched tier by a direct `UPDATE` so the tier
*label* changed and the composed document did not. It is a conservative floor
rather than an estimate, because a name-shaped document selects a name-shaped
pool, which weakens exactly the correlation being measured — and for the same
reason it is a floor over *that* population and **not a baseline** for a run
over a differently-selected, genuinely enriched one. **This ADR is about
the shape, not the weight**, and the shape is right at any coverage; if the
term is eventually removed, the table is still the cheapest form of a signal
worth keeping around. ✅ **It was removed on 2026-08-12 and the table stayed —
see the amendment below**, which is this sentence's own prediction discharged
rather than a reversal of it.

**Nothing verifies the matrix is still dense except the importer.** If a future
release ships a sparse genome, the right answer is probably still a dense
vector with a sentinel — but that is a decision nobody has had to make, and the
importer failing loudly is what buys the chance to make it.

---

## Amendment, 2026-08-12 (M9 Task S7) — the term is removed; the table, the read and the counter stay

**The shape this ADR argues for is unchanged and unchallenged. What changed is
the *weight*, which this document never owned and which
[05](../05-search-and-similarity.md) and [09](../09-roadmap.md) deferred to M9
by name.** The amendment lands here because the Uncertainty section above is
where the open question was recorded, and a question answered somewhere else
is one the next reader re-opens.

### The measurement

M9's S5 walked the whole embedded population **once**, read-only, over the pool
`SimilarityService.rebuild()` itself draws — `list_embedded` →
`nearest_for(page, limit=_CANDIDATE_POOL)` — counting over the **pool** and
never over stored rows, and writing nothing. On a verified-idle box:

| | |
|---|---|
| seeds walked | **130,647** (262 pages of 500) |
| candidate pairs considered | **13,064,700** |
| pairs carrying a genome vector on **both** sides | **323,297** |
| **candidate-pair rate** | **2.4746%** |
| seeds carrying a vector | **15,525** — 11.883% single-side |
| `coverage²` (the wrong arithmetic) | 1.412%, i.e. the measurement is **1.75×** it |
| wall clock | 5,125 s (85.4 min) |

The genome's 15,565 stored rows reconcile to those 15,525 seeds with **no
residue**: 15,525 embedded + **7** still `skeleton` (so `_POPULATION` never
owed them an `index` job) + **33** outside the frozen tier = 15,565.

### It is a second measurement, not a rise from M7's 5,020-seed 1.81%

M9's S1 settled that M7's 1.81% came from **5,020 owned, name-selected,
pre-TMDb seeds** in a scratch database that no longer exists: the promotion to
the enriched tier was a tier-label `UPDATE` that moved no document, so
`search_document`'s weight classes C and D were empty and `nearest_for`
selected the pool **by name**. Both figures stay in the record with their
populations attached, and neither is a baseline for the other.

**What 2.4746% newly says is what the genome does over documents that finally
carry `overview`, `tagline`, `genres` and `keywords`** — which is precisely
what M9's 130,647-title enrichment existed to produce. And it is still **four
times below the 10% floor the 0.25 weight assumes.**

### The decision

`SimilarityService._WEIGHTS` loses its `"tags"` key and `_neighbors_for` stops
passing `tags=` to `_blend`. **Everything else stays**: `genome_scores`,
`genome_tags`, `GenomeRepository`, the pairwise statement in
`db/repositories/search.py`, `NeighborCandidate.tags`, `NeighborSeed.
has_genome`, and `NeighborRebuild`'s `seeds_with_genome` /
`pairs_with_tags` / `candidate_pairs` counters, which `usher similar --rebuild`
prints.

**Keeping the read is a decision and not an oversight.** The removal saves the
blend arithmetic; it does **not** save the `<=>` and the TOAST fetch per
candidate pair, and PRD 05's cost sentence is corrected accordingly rather than
quoted as a saving. What the read buys is that the number a later milestone
would re-open this on is produced by the path that would consume the vectors,
on every rebuild, without anybody thinking to run a query — the same argument
that put the counter there in M7. It is also the *only* remaining consumer of
ADR-0014's `None`-rather-than-0.0 rule on this field: a port answering `0.0` for
a half-covered pair would report a barely-covered catalog as fully covered,
making a dead signal look live.

### What the removal actually moves, exactly

`_blend` renormalises over present signals, so for any pair, with `W` the
weights of the non-genome signals present and `g` the genome cosine:

```
score_with_genome = (W · score_without_genome + 0.25 · g) / (W + 0.25)
```

With all three other signals present `W = 0.75` and that is exactly
`0.75 · score_without + 0.25 · g`. Three consequences, all arithmetic:

- **On the ~97.5% of pairs carrying no genome, the removal changes no score at
  all.** The three surviving weights are left at M7's 0.45 / 0.20 / 0.10, and
  `_blend` divides by their sum — so those pairs are scored under precisely the
  denominator they already were.
- **On the 2.4746% that do, the term was a promotion whenever `g` exceeded the
  pair's score on everything else**, and a demotion otherwise. The genome
  cosine's own distribution is measured in the Evidence above: **min 0.2556,
  p1 0.4075, p50 0.6095, mean 0.6101** over 268,157,000 ordered pairs. So a
  genome-bearing candidate whose other signals score it below 0.4075 was being
  promoted with 99% probability, and genome coverage concentrates in popular,
  older, heavily-embedded films — S5 measured pool membership and genome
  membership as positively correlated at **1.75×**.
- ⚠️ **The distribution of the three-signal score over a real pool is NOT
  measured**, so how often that promotion actually reordered a list is unknown
  and is not claimed here. `title_neighbors` holds **0 rows** on every catalog
  on this host. The identity above is exact; the frequency is not.

### Why the three surviving weights are not "reverted to M6's"

[09](../09-roadmap.md) phrased the deferred choice as *"a genome-aware
candidate pool or reverting `_WEIGHTS` to M6's three signals"*. The pool option
is out of scope by M9's own boundary (`_CANDIDATE_POOL` feeds
`blend_fingerprint`, and the group builds no new statement), and **the revert
is taken in the narrow sense only**: the genome term comes out and
`cosine`/`keywords`/`genres` stay at 0.45 / 0.20 / 0.10 rather than returning
to M6's 0.60 / 0.25 / 0.15.

The measurement licenses removing a term whose coverage cannot support its
weight. It licenses **nothing** about keywords against genres, which is the
only thing the two spellings differ on — 0.45/0.20/0.10 renormalises to
0.600 / 0.267 / 0.133 and M6's to 0.600 / 0.250 / 0.150. Restoring M6's numbers
would be an unevidenced second decision riding on an evidenced first, and it
would move **every** score in the table rather than only the ones the evidence
is about. The residual is bounded at **±0.0167** either way and is pinned by
`test_every_pair_is_scored_within_m6s_reweighting_bound`.

### Removed, not set to 0.0

`_blend` adds `_WEIGHTS[name] * value` to the numerator **and**
`_WEIGHTS[name]` to the denominator, so **a 0.0-weighted signal is
arithmetically the same program as an absent one, to full precision, at every
value the signal can take.** It is therefore invisible to every behavioural
assertion — while still entering `blend_fingerprint()`, declaring every stored
row stale and buying an 85-minute rebuild for a table whose every score is
unchanged. The key and the call-site argument move **together**: `_blend` looks
up `_WEIGHTS[name]` for every signal it is handed, so removing the key alone is
a `KeyError` on the first pair of the first page.
`test_a_zero_weight_signal_is_arithmetically_identical_to_an_absent_one`
demonstrates the equivalence and
`test_every_signal_the_blend_is_handed_has_a_weight_and_no_weight_is_zero`
forbids both careless spellings structurally, since neither is visible in a
score.

### The ceiling no enrichment can move

This is the half that makes the decision unlikely to reverse on more
enrichment. `ml-latest` is **movies-only**, **frozen at 2023-07-20**, and
carries genome scores for **16,376** movies — **18.9% of its own 86,537-movie
list** ([04](../04-catalog-bootstrap.md), the dataset table and the `links.csv`
padding note). `ml-32m`, the newest full release, **dropped the genome
entirely**; `ml-25m` still has one and forbids redistribution. So the genome's
coverage of this catalog is bounded above by a file that has not moved in three
years and cannot cover television at all, and it falls every year whether or
not anyone touches this table. M9's enrichment moved the *document* and
therefore the *pool*; it could not and did not move the numerator.

### The freed name `tags` is a trap, resolved here rather than in a commit message

`_WEIGHTS`, `NeighborCandidate.tags` and `NeighborRebuild.pairs_with_tags` all
spell the **tag genome** as `tags`, and M9's S6 evaluated a *different* signal
— MovieLens **user tags** — under the same word (refused at 6.0821%, ADR-0035).
**A future user-tag term must not be called `tags`.** A stored
`title_neighbors.score` records only a `blend_fingerprint`, so a later reader
who found `tags` back in `_WEIGHTS` at some other weight would have no way to
tell which of the two signals a row's score contains. The rule: the genome key,
if it ever returns, is `genome`; a user-tag term is `user_tags`; and either way
`blend_fingerprint()` moves, which is what keeps the two eras of rows apart.

### What this does not say

**Nothing here measures whether the genome made a neighbour list *better*.** A
candidate-pair rate is a statement about **membership** — it can say a term
fires too rarely to be worth its weight and it cannot say 0.20 beats 0.25. That
would need relevance judgements this project has never had, and the caveat M7
attached to the 0.25 weight applies unchanged to its removal.

### The obligation this creates

A weight change invalidates every row of `title_neighbors` by design —
`blend_fingerprint()` moves from `78900b2bd89a649774d7fd3efe082621` (M7/M8's
four signals) to `78f3ecd20e654c0f6aa4bdf646ec099b`. That is
[ADR-0020](0020-derived-state-carries-its-fingerprint.md) working: staleness is
a **query**, `SimilarityService.stale_neighbors()` answers it, and
`usher similar --rebuild` is the only repair. At 130,647 embedded titles the
rebuild is a full quadratic walk priced at **~80 minutes** (S4) and measured at
**85.4** (S5), so it is a scheduled operation rather than the tail of a task —
and it is **not run by this change**. M9's H7 owns it, and the criterion has to
carry a row count beside the verdict, because *"`blend_fingerprint` reports no
stale rows"* is satisfied by an empty table, which is what every catalog on
this host holds today.
