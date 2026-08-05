# ADR-0024 — The tag genome is one dense vector per title, not a tall relevance table

**Status:** Accepted — corrects PRD 02's implied shape

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

**The tag vocabulary is not stored.** Nothing in M7 reads a tag *name*: a
cosine needs the two vectors and the guarantee that their positions mean the
same thing. `genome-tags.csv` is read by the importer to verify contiguity and
width, then thrown away.

## Consequences

**Gained:**

- **The similarity term is a single `<=>`** — the operator `SimilarityService`
  already blends for embedding cosine, so the fourth signal costs an accessor
  and a weight rather than a new code path.
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

- **M8 inherits the vocabulary, and the cost is recorded rather than
  discovered.** An LLM prompt that wants to say *"atmospheric,
  thought-provoking"* needs the words. Paying for it is a 1,128-row table plus
  a loader step in a phase that already reads the file, and one migration —
  and `genome_revision` is what makes it safe rather than a
  deferral-by-omission: the vocabulary M8 loads must carry the same revision as
  the vectors it explains, and there is already something to check it against.

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
squared, on a catalog with no TMDb enrichment. It is a conservative floor
rather than an estimate, because a name-shaped document selects a name-shaped
pool, which weakens exactly the correlation being measured. **This ADR is about
the shape, not the weight**, and the shape is right at any coverage; if the
term is eventually removed, the table is still the cheapest form of a signal
worth keeping around.

**Nothing verifies the matrix is still dense except the importer.** If a future
release ships a sparse genome, the right answer is probably still a dense
vector with a sentinel — but that is a decision nobody has had to make, and the
importer failing loudly is what buys the chance to make it.
