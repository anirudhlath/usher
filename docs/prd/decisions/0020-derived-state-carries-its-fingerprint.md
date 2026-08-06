# ADR-0020 — Derived state is fresh by construction, or carries its fingerprint

**Status:** Accepted. Implemented in M6.
**Date:** 2026-08-02

## Context

Every milestone before this one shipped state that something *wrote*. M6 is
the first to ship state that is **derived**, and derived state has a failure
mode the others do not:

> **A stale index does not raise. It answers.**

A `Title` enriched last night and never re-indexed produces no error, no log
line, no failed job and no degraded health check. It produces a result set
that is quietly, confidently wrong — missing the film added an hour ago,
ranking on an overview that has since been replaced. There is no timeout to
catch it and no exception to translate.

It is the M5 lesson in a new costume.
[ADR-0018](0018-push-health-is-a-message-ledger.md) exists because a channel
can look healthy while delivering nothing; here an index can look healthy
while describing yesterday. The answer is the same shape: **do not infer the
property — record the thing that makes it checkable.**

## Decision

**Nothing in M6 infers freshness. Every derived artefact is either fresh by
construction, or carries the fingerprint of the input it was derived from, so
that staleness is a query.**

The two halves of [PRD 03](../03-sources-and-sync.md)'s stage 4 land on
opposite sides of that rule, and **the asymmetry is chosen rather than
drifted into**:

- **The full-text document is a `GENERATED ALWAYS AS (…) STORED` column on
  `titles`.** Not a trigger, not a job, not a queue. PostgreSQL recomputes it
  inside the same statement that writes `name`, `original_name`, `overview`,
  `tagline`, `genres` or `keywords`, so there is no code path — bulk `COPY`,
  hand-written `UPDATE`, future migration — that can write a title and skip
  its document. **Half the freshness problem is deleted rather than solved.**
- **The embedding needs a model, so it cannot be generated.** It is a
  `JobKind.INDEX` job, and jobs fail, park, or are never enqueued. Rather
  than trusting the queue, `title_embeddings` records *what* was embedded
  (`source_fingerprint`, the `md5` of the exact assembled text) and *by what*
  (`model_name`, the runtime **and** the checkpoint —
  `fastembed:BAAI/bge-small-en-v1.5`; see
  [ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md)).

Stale is then one predicate:

```sql
e.title_id            IS NULL
OR e.model_name         IS DISTINCT FROM :model_name
OR e.source_fingerprint IS DISTINCT FROM md5(<the same assembly the composer uses>)
```

`STALE_EMBEDDING` in `usher.db.repositories.search`, **imported by every
consumer and restated by none** — a predicate written twice is two
predicates, and that is how a backfill stops draining. It has three
consumers: the backfill's keyset cursor, the `usher.search.embeddings.stale`
gauge, and the test that proves the enqueue-on-enrichment path actually
closes.

## Consequences

**Gained.** A backfill that is a *predicate* rather than a cursor over
everything is self-draining, idempotent and re-runnable at zero write cost —
and combined with `enqueue`'s existing
`WHERE jobs.status <> 'parked' AND jobs.priority < excluded.priority`,
re-running it writes **no rows at all**. A model swap invalidates every
vector automatically, which is the scheme replacing a migration. Editing a
title's overview re-claims that one row with nothing being told.

Coverage is *reported* rather than assumed: `SearchOutcome.semantic_coverage`
is the fraction of the filtered population that actually had a vector, and a
`FUSED` search against a catalog with zero embeddings degrades to full-text
**and says so** (`requested_mode` beside `mode`), rather than silently
becoming full-text with a confident-looking blended score.

**Given up — the generated column is computed on every write of `titles`,
and unlike `bulk.py`'s `_SUSPENDABLE_INDEXES` it cannot be suspended.**
Measured 2026-08-02 against `pgvector/pgvector:pg17`, 300,000 synthetic
skeleton-shaped rows through `INSERT … SELECT` (the bootstrap's own statement
shape):

| | plain `titles` | with the generated column |
|---|---|---|
| `INSERT … SELECT` 300k rows | 734 ms | **2,980 ms (4.06×)** |
| total relation size | 57 MB | 76 MB (**+33%**) |

Extrapolated to 1,271,138 titles: **about +9.5 seconds and about +80 MB** on
a whole-catalog bootstrap already measured in minutes. That is noise, and it
buys a guarantee no amount of code can equal. **Two costs are not in that
figure and are not measured**: the GIN index's own write cost, and
`apply_ratings`' `UPDATE` over 538,937 rows, each of which recomputes a
`tsvector`. The named fallback — a `title_search_documents` side table
maintained by a trigger, which *would* be suspendable — is not taken, because
it is code and code has bugs, which is the whole thing being bought.

**Also — PRD 05's expression did not compile, and the fix has a hazard.**
`GENERATED ALWAYS AS (…) STORED` rejects the documented expression with
`ERROR: generation expression is not immutable`, because
`array_to_string(anyarray, text)` is `STABLE`: `anyarray` admits element
types whose output depends on a GUC (`timestamptz` and `TimeZone`).
`array_to_tsvector` is the obvious immutable replacement and is **wrong** —
it emits array elements as raw, unlexized, case-preserving lexemes, so a
genre search silently matches nothing. The shipped formulation is a custom
`IMMUTABLE` SQL wrapper narrowed to `text[]`, `usher_array_text`.

**And a custom function in a generated column carries a verified trap.**
`CREATE OR REPLACE FUNCTION` **does not recompute stored generated values** —
demonstrated directly: a row stored as `'alpha':1 'beta':2` did not move when
the body changed, while a fresh evaluation returned something else entirely.
Worse, a subsequent `UPDATE` of that row *did* recompute it with the new
definition. So replacing the body silently produces a table where **some rows
were computed by the old definition and some by the new, with nothing to tell
them apart** and the split decided by which rows happened to be touched
since. That is this ADR's own failure mode arriving through the back door, so
it gets this ADR's own treatment — made structural, not remembered:

1. Any migration that changes the wrapper's body **must force a full rewrite
   of the column in the same migration** (drop the index, drop the column,
   replace the function, re-add the column, recreate the index) and say so in
   its docstring. Migration `fa2b6c1e9d30` carries the recipe.
2. `tests/integration/test_search_document.py::
   test_the_stored_document_equals_a_freshly_computed_one` samples rows and
   asserts the stored document equals a freshly computed one. It is cheap,
   and it is the only thing standing between the wrapper and a silently
   mixed-state table.

**Also — the degenerate-document trap, and the second trap the fix creates.**
Measured: **every whitespace-only input embeds to the *identical* vector** —
cos(`""`, `" "`) = cos(`""`, `"\n"`) = **1.0000, exactly**. A title whose
composed document comes out empty is not a bad result; it is a perfect unit
vector satisfying every clause of the port contract, sitting at cosine 1.0
from every *other* empty-document title. That is a degenerate cluster of
unbounded size pinned to the top of every "more like this" result, and no
assertion about norms, dimensions or determinism can see it. So the composer
**refuses** to emit a degenerate document.

**The refusal creates the worse trap.** A refused title with no row keeps
matching the stale predicate forever: the backfill re-claims it every pass,
the gauge never reaches zero, and the queue churns permanently on rows that
can never succeed. **This project has shipped exactly that bug once already**
— the watch-history repair whose merge carried the walk's instant was refused
by the very row it existed to repair, and matched `played AND play_count = 0`
for good. Hence:

> **A refusal is a written outcome, not a skipped one.**

A refused title gets a `title_embeddings` row with a `NULL` embedding, the
current `model_name`, and the fingerprint of the degenerate text. It stops
matching the stale predicate, starts matching a separate countable one
(`REFUSED_EMBEDDING`, `NOT (STALE_EMBEDDING) AND e.embedding IS NULL` — the
negation is load-bearing, since a bare `embedding IS NULL` also matches rows
refused under an *older* model, and the two counters would then sum above the
population), and is re-claimed exactly once when enrichment gives it content.

**Also — a derived column on `titles` collides with the 1:1 row/model rule,
in three places and not one.** `Title` is `extra="forbid"`, so
`TitleRow.__table__.columns` and `Title.model_fields` have to agree.
`DERIVED_COLUMNS` on `TitleRow` is the declared exception, and membership in
it is the deliberate act — an undeclared column still breaks every read,
loudly, which is the property the rule exists for. The site that gets missed
is the second: `update()`'s mutation loop `setattr`s every column, so without
the exclusion Postgres answers `ERROR: column "search_document" can only be
updated to DEFAULT` — on **writes**, which a task that only tested reading a
seeded row would never see.

**Rejected**, three, each with why:

- ***Rebuild the document in the `index` job alongside the embedding.*** The
  obvious symmetry, and wrong: it makes the cheap, always-correct half depend
  on the expensive, fallible half. A parked embedding job would then also
  mean a stale full-text document, and the two failures would be
  indistinguishable.
- ***Trust the enqueue and skip the fingerprint.*** The enqueue is one line
  in `EnrichService._apply`. Every path that writes a title without going
  through it — a migration backfill, a repair script, a future source of
  catalog updates — produces a silently stale vector with nothing to detect
  it. The fingerprint costs one `md5` and makes correctness observable
  instead of assumed.
- ***A `search_version` integer bumped on write.*** Detects that something
  changed but not *what*, so a model swap and a text edit are the same
  signal, and re-embedding after a model change would need a full sweep with
  no way to confirm it finished.

## Evidence

All measurements 2026-08-02 on this host against `pgvector/pgvector:pg17`
(PostgreSQL 17.10, pgvector 0.8.6), corpora synthetic.

- **The bootstrap regression**: 734 ms → 2,980 ms (4.06×) and 57 MB → 76 MB
  over 300,000 rows; ≈ +9.5 s and ≈ +80 MB extrapolated to 1,271,138 titles.
- **The `CREATE OR REPLACE` hazard**: stored values not recomputed on the
  function change, recomputed on a later row `UPDATE` — the mixed-state
  table, demonstrated rather than feared.
- **The whitespace degeneracy**: cos(`""`, `" "`) = cos(`""`, `"\n"`) =
  1.0000 exactly.
- **The control that says the refusal threshold is about *empty*, not about
  *thin***: unrelated name-only skeleton documents measure pairwise cosine
  **0.5867 (sd 0.055)**, and a skeleton retrieves its own enriched form at
  **0.7638** against a **0.4751** cross-title mean. Crowded, but ordered. A
  thin document is a poor document; an empty one is a different object.
- **The scheme, pinned end to end**, each case naming the wrong
  implementation it fails:
  `tests/integration/test_search_repository.py` —
  `test_a_title_with_no_embedding_row_is_stale`,
  `test_a_model_change_makes_every_row_stale_again`,
  `test_editing_a_title_makes_it_stale_without_anything_being_told`,
  `test_a_refused_title_is_counted_as_refused_and_not_as_stale`,
  `test_the_composer_refuses_exactly_the_titles_the_refused_predicate_finds`,
  and `test_the_composer_and_the_sql_fingerprint_agree` — which is the one
  that matters most, because `_FINGERPRINT_SQL` is a **second
  implementation** of the composer and the two drifting apart is failure mode
  (a) of the whole scheme.

## Uncertainty

**The fingerprint proves the *text* is current. It cannot prove the *assembly
rule* is.** A change to the document composer that does not change any
title's text — a reordering of the fields, a different separator — produces
identical fingerprints over different documents, and every stored vector
silently describes the old assembly. In M6 that is unreachable: the composer
is one function with one caller, and
`test_the_composer_and_the_sql_fingerprint_agree` pins it against the SQL
twin. The honest answer for the day it *is* reachable is a manual sweep, and
it is written here rather than implied to be covered.

**`title_neighbors` was an acknowledged exception to this ADR's own rule.
M7 closes half of it, and the half it does not close is stated rather than
implied.**

M6 recorded the exception like this, and this part is still correct: a
title's neighbours go stale when *some other* title gets an embedding, and
there is no per-row predicate that can decide that without recomputing the
row. It carried an oldest-row `computed_at()` instead, where `None` means
never computed, and it was rebuilt rather than repaired.

**What that argument missed is that there are two causes of staleness, not
one — and M7 made the second one urgent by doing it.** M7's similarity work
added the tag-genome term and re-weighted the other three, so every row
written before it is a three-signal blend and every row after is a four-signal
blend. Both are in `[0, 1]`, both carry a plausible `rank`, both sit in one
table, and **nothing distinguished them.** That is exactly the state this ADR
exists to eliminate, arriving in the one artefact it had exempted.

So `title_neighbors` now carries **`blend_fingerprint`** — the md5 of
`_WEIGHTS`, `_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL`, i.e. of the three
constants that between them decide what a stored score *means*. One definition
(`services/similar.blend_fingerprint()`), three consumers: `usher similar
<title id>` says so per title, `usher.similarity.neighbors.stale` counts the
table, and `usher similar --rebuild` drives it to zero. Migration `ffb` stamps
every pre-existing row with M6's own fingerprint
(`6697a3e1eaca411cbae890e54a4c665a`) rather than a sentinel, so those rows
*name* the blend that computed them instead of merely failing to match.

**It does not answer "has some other title been embedded since?"** That half
is genuinely undecidable per row, `computed_at()` still exists beside the
fingerprint for it, and **nothing schedules `usher similar --rebuild`.** Two
causes, one closed, and saying which is the difference between an improvement
and a claim. A freshness predicate that looked like the others and did not mean
the same thing would still be worse than an honest gap.

**This is also the milestone that made this ADR's own Uncertainty section
live.** The paragraph above warns that a fingerprint proves the *text* is
current and not the *assembly rule*. M7's weight class B moved every
`search_document` fingerprint at once, deliberately, and the blend fingerprint
is the same idea applied to an assembly rule that has no text to hash: when the
rule itself is the input, hash the rule.

**Nothing measures the freshness of the *whole* system end to end.** Each
half is checkable, and no gauge says "the index as a whole describes the
catalog as of now" — that would be the third derived artefact, and it would
need the same treatment.
