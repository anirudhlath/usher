# ADR-0038 — The embedding width is deployment-wide DDL, and the fingerprint's "no migration" stops at it

**Status:** Accepted. Implemented in `m09e`, after M9 — **narrows
[ADR-0020](0020-derived-state-carries-its-fingerprint.md) and
[ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md)**, and
corrects [02](../02-data-model.md), [04](../04-catalog-bootstrap.md),
[05](../05-search-and-similarity.md) and [08](../08-operations.md).
**Date:** 2026-08-13

## Context

Three facts arrived together and only the first reads like a preference.

1. **`fastembed` cannot serve `BAAI/bge-m3` at all.** Enumerated 2026-08-13 on
   fastembed 0.8.0 across all five of its model classes — `TextEmbedding`,
   `SparseTextEmbedding`, `LateInteractionTextEmbedding`, `ImageEmbedding`,
   `LateInteractionMultimodalEmbedding` — and `bge-m3` appears in none of them.
   So the question was never *which runtime is nicer*. It was whether this
   deployment wants `bge-m3` or wants an in-process model, and the local vLLM it
   already runs can serve the former.
2. **`bge-m3` is 1024 wide against a `halfvec(384)` column.**
   `EMBEDDING_DIMENSIONS` was a literal in `db/models/search.py`, and
   `FastEmbedEmbedder._DIMENSION = 384` was a *second* declaration of the same
   number that agreed with it by coincidence.
3. **[ADR-0020](0020-derived-state-carries-its-fingerprint.md) and
   [ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md)
   both state that a model swap needs no migration.** That claim had stood for
   seven milestones and had never been tested by an actual swap.

Fact 3 is the one worth an ADR. It is not wrong; it is **narrower than it
reads**, and the first swap this project performed is the one that walks off its
edge.

## Decision

### Part 1 — a second `Embedder`, chosen by a runtime prefix, not a replacement

`OpenAICompatEmbedder` (`adapters/embedding/openai_compat.py`) issues
`POST {embedding_base_url}/embeddings` and is selected by the `openai:` prefix on
`Settings.embedding_model`; `fastembed:` still selects `FastEmbedEmbedder`, which
ships unchanged in every respect ADR-0022 argued for. The capability-named
directory needed no rename, because it was never named after the thing that
turned out to be replaceable.

Three calls inside it are decided rather than defaulted:

- **Order is re-established, never assumed.** `fastembed` answers positionally;
  this protocol answers with objects carrying an `index`, precisely because
  arrival order is not part of the contract. The vectors are sorted on that
  `index` and the index set is asserted to be exactly `range(len(texts))` — a
  count check alone is satisfied by a duplicate, and a duplicate is a missing
  vector wearing another one's number. `Embedder.embed`'s docstring already
  names the consequence: title *n*'s vector lands on title *m*, and no
  per-vector assertion can see it afterwards.
- **An unrecognised prefix raises at startup rather than falling back.**
  `USHER_EMBEDDING_MODEL=openia:BAAI/bge-m3` under a fallback would embed the
  catalog with whatever `fastembed` made of the string and write
  `openia:BAAI/bge-m3` into `title_embeddings.model_name` — the fingerprint
  would then record a model that never ran and the stale predicate would report
  the deployment as current. A typo has to be loud here *because* the
  fingerprint is trusted everywhere else. A bare checkpoint with no prefix is
  not an unknown runtime: it is `fastembed`, matching `checkpoint_of`'s leniency
  in both adapters.
- **`dimensions` is not sent on the wire.** A provider that honours it would
  silently truncate to whatever this deployment asked for and make Part 5's
  width check agree with itself; a provider that does not is a 4xx on every
  request. **The width is a fact to check, not a thing to request.**

### Part 2 — the width is one deployment-wide constant, so `m09e` is DDL

`EMBEDDING_DIMENSIONS` is `halfvec`'s typmod on `title_embeddings.embedding`
**and** on `user_taste.centroid`, and a typmod is DDL. `m09e` moves both columns
384 → 1024 together, because a centroid is a mean of vectors from the other
column and is compared against it.

**Every affected row is deleted rather than converted, and the deletes precede
the `ALTER`.** There is no honest conversion — a 384-lane vector padded or
projected to 1024 is not what the new model would have produced — and
`halfvec(384) → halfvec(1024)` is a runtime dimension error the moment one row
exists.

**Deleting the rows is the correct end state and not merely the reachable one,
and the distinction is load-bearing for `title_embeddings`.** A row there with a
`NULL` embedding is a *written refusal* ([ADR-0020](0020-derived-state-carries-its-fingerprint.md)),
so nulling the column instead of dropping the row would mark every embedded
title permanently refused and the backfill would never claim one of them again.

The migration spells `1024` and `384` as literals rather than importing
`EMBEDDING_DIMENSIONS`. `ffa` imports the constant and is the precedent this
departs from on purpose: a revision records what it *did*, and one whose DDL
changes meaning when a constant is next edited cannot be replayed. `ffa` was
minted when the number had never moved.

### Part 3 — the fingerprint's promise is narrowed, in every place that made it

The mechanism is intact and is **scoped to a swap at one width**. Within a width,
changing `Settings.embedding_model` still stales every row through
`e.model_name IS DISTINCT FROM :model_name`, the backfill re-claims them, the
gauge climbs and drains, and nobody writes a migration. Across widths there was
never a claim — only the absence of a counterexample, which `m09e` supplies.

Read as narrowing rather than contradicting. The wording is corrected at
`db/models/search.py`'s constant, `ports/embedding.py`'s `model_name` and
`dimension` docstrings, `.env.example`, PRD [02](../02-data-model.md) and PRD
[05](../05-search-and-similarity.md), and in this ADR's two parents — not only
in the paragraph that prompted the amendment.

### Part 4 — the service-free default moves to `bge-large-en-v1.5`, and it costs 1.2 GB

Because the width is deployment-wide rather than per-model, a 384-wide
checkpoint can no longer be stored **at all**, so
`fastembed:BAAI/bge-small-en-v1.5` could not remain the default. Of the
1024-wide models fastembed 0.8.0 actually ships — enumerated in the same pass as
fact 1 — `BAAI/bge-large-en-v1.5` is the only well-measured English one.

**It is 1.2 GB against bge-small's 0.07 GB.** That is a real regression in the
one install ADR-0022 exists to protect — the household that runs no inference
server — and it is stated as a price rather than as a detail. It is not paid by
a deployment on the `openai:` runtime, which holds no model on this disk at all.

### Part 5 — a width mismatch narrows the deployment; it does not refuse to boot

`composition.embedder` compares `Embedder.dimension` against
`EMBEDDING_DIMENSIONS`, and on disagreement closes the embedder, logs once and
returns `None` — the same outcome as an absent model, so `INDEX` jobs go
unclaimed and nothing else moves.

Two halves, and both were arguable:

- **`Embedder.dimension` must report the model's own width, never the schema's.**
  `FastEmbedEmbedder` now reads `TextEmbedding.embedding_size` off the loaded
  model instead of a literal, and `OpenAICompatEmbedder` takes the expected width
  and asserts it on the first batch. Returning `EMBEDDING_DIMENSIONS` from an
  implementation makes every implementation agree with the column by
  construction and turns the check into `x == x`. **The whole value of the
  property is that it can disagree.**
- **Narrowed rather than fatal.** A wrong width is a misconfiguration and not an
  absent capability, which argues for a raise. The deciding fact is that the
  *consequence* is identical — no model, no index jobs, nine of ten row
  providers and the whole catalog-lookup tier unaffected — so refusing to boot
  would take a working deployment down over a setting only the index lane reads.
  This is [08](../08-operations.md)'s degradation rule, applied where ADR-0022
  already applied it.

What the check buys is **where** the mismatch is reported. Without it, a
384-wide checkpoint against a 1024-wide column is discovered one failed `index`
job at a time, in a worker log, in an asyncpg message about a vector that names
the width on neither side.

### Part 6 — `title_neighbors` is emptied, and that fixes the instance rather than the class

`title_neighbors` holds no vector, so nothing in the DDL forces its hand. It is
emptied because every row in it was computed from vectors this revision
destroys — and **`blend_fingerprint()` cannot tell.** It hashes `_WEIGHTS`,
`_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL`: what a score *means* in the
blend's terms. The embedding model is not one of its inputs, so a model swap
leaves every neighbour row reading as current, in `[0, 1]`, with a plausible
`rank`, derived from a model the deployment no longer runs, and
`usher.similarity.neighbors.stale` reading zero throughout.

That is the defect ADR-0020 and the `blend_fingerprint` column were introduced
to close one milestone earlier, arriving through a door nobody checked. **The
class fix is to feed the embedder's `model_name` into `blend_fingerprint()`,
which changes its signature and all three of its consumers. It is not done**,
and it is recorded as the follow-up in
`.claude/rules/search-and-embeddings.md` rather than smuggled into a width
migration.

## Consequences

**The catalog is left empty of every derived vector artefact, and none of it
comes back on its own.** Zero embeddings, zero taste centroids, zero neighbours.
`usher index --backfill`, then `usher work` until the queue drains, then
`usher similar --rebuild` — which, as `CLAUDE.md` already says, nothing
schedules.

**`downgrade()` is symmetric and equally destructive.** It restores the width
and not the data, because the data it would restore was 1024 wide.

**Three settings, all read only by the `openai:` runtime.**
`USHER_EMBEDDING_BASE_URL` (default `http://localhost:8001/v1`),
`USHER_EMBEDDING_API_KEY` (a `SecretStr`, empty meaning *send no `Authorization`
header at all* rather than an empty bearer token) and
`USHER_EMBEDDING_TIMEOUT_SECONDS` (30.0, bounded because an embedder that hangs
holds a worker slot and `JobWorker` has no timeout of its own).
**`embedding_base_url` is deliberately not `llm_base_url`**: they are one
endpoint on many hosted providers and two processes here, because vLLM serves
one model per process. Collapsing them would make "point the embedder somewhere
else" impossible without moving curation too.

**Given up — the HNSW index is rebuilt at `m09e`'s parameters and its price is
not the measured one.** `m=16, ef_construction=64` and the same partial
predicate are carried over unchanged, but M6's *50,000 × halfvec(384) in 4.109 s
into 56 MB* is now a measurement of a narrower vector than the one being
indexed: 1024 lanes is 2,048 bytes against 768, so M6's 1,170.5 bytes/row
projection no longer applies. Re-measured after the backfill rather than guessed
here — see *Uncertainty*.

**Rejected: keeping the 384 column and declining `bge-m3`.** That is the option
this ADR spends 1.2 GB of default install and a destructive migration to avoid,
and it is a live option for anyone reading this: the ranking baseline in
*Uncertainty* is what the 384 default actually delivered, and nothing here has
yet measured that 1024 delivers better.

## Evidence

**The enumeration**, 2026-08-13, fastembed 0.8.0: all five model classes listed
and searched; no `bge-m3` in any. This is the fact the whole change rests on and
it was run rather than assumed.

**Normalisation holds on the served model.** `BAAI/bge-m3` through vLLM returns
norm **exactly 1.0** (2026-08-13), so `_NORM_TOLERANCE = 1e-4` has four orders
of magnitude of headroom against the 8.99–9.46 a missing `Normalize` module
produces — ADR-0022 Part 3's failure mode, re-checked against the new runtime
rather than inherited. `EmbedderContract` cannot cover this and covers it *less*
well here than for `fastembed`, because the served model is the one thing about
this adapter that can change while the process lives.

**The live database, immediately before and after `m09e`** (2026-08-13, the
development catalog at schema `m09d`):

| | before | after |
|---|---|---|
| `title_embeddings.embedding` / `user_taste.centroid` | `halfvec(384)` | `halfvec(1024)` |
| `title_embeddings` rows | **130,673** | 0 |
| `ix_title_embeddings_hnsw` | **146 MB** | recreated empty, same `m`/`ef_construction`/predicate |
| `title_embeddings` total relation | **278 MB** | — |
| `title_neighbors` rows | **3,266,175** | 0 |

**The test suite's width was a literal in eight fixtures and one of them was
load-bearing.** `_EMBEDDED_ROWS` had always depended on the width without saying
so: only `2 × rows / width` rows are closer than a tie to a basis-vector probe,
and 1024 lanes put that under `ef_search`, firing the HNSW premise guard as
`assert 100 < 100`. Bisected on the container — 15,000 fails, 20,000 passes —
and shipped as `26 × width`, the ratio the 384 fixture always had. **A premise
guard that fires on a width change is the guard working**; it is recorded here
because the obvious reading was "the migration broke HNSW".

Deployment facts that are not decision evidence but cost real time — the two
vLLM engines this host now runs, `--load-format pt` loading 391 uninitialised
weights, and `--served-model-name` producing a permanent HTTP 404 park — are in
`.claude/rules/search-and-embeddings.md`.

## Uncertainty

**The re-embed of the enriched tier is running as this is written, and three
numbers are owed rather than unknown-in-principle: the backfill's throughput
through `bge-m3`, the rebuilt HNSW index's size at 1024 lanes, and whether
semantic ranking actually improves.** None of them is written here, in any
direction, because none has been measured; a `TBD` would read as a gap somebody
forgot rather than as a run in progress. They belong in
`.claude/rules/search-and-embeddings.md` beside the 384 figures they replace,
added when the run finishes.

**The baseline the third of those has to beat is on record and it is not
flattering.** Measured over this catalog with `fastembed:BAAI/bge-small-en-v1.5`:
a plot-description query puts the correct answer in the top **0.05–0.3%** of the
*embedded* population — the enriched tier, ~130k rows — and usually **outside
the top 20**. *"A man relives the same day over and over"* ranked Groundhog Day
**64th**, Shawshank **208th**, The Matrix **262nd**, WALL-E **338th**, while a
query naming a title's subject matter directly put Jurassic
Park **1st** and a Harry Potter query **4th**. So the failure is specific: the
model retrieves *topic* well and *plot* poorly, and 1024 lanes of `bge-m3` is a
bet on that particular gap. **Nothing in this ADR is evidence that the bet
pays.**

**Whether M8's query-expansion result survives the model change is unmeasured.**
The MRR 0.733 → 0.373 run ([05](../05-search-and-similarity.md)) was taken with
`fastembed:BAAI/bge-small-en-v1.5`, and its diagnosis — generic critic prose
collapsing toward the corpus centroid — is a claim about an embedding space that
this change replaces. The default stays off, because a measurement is not
reversed by a change that did not re-run it.

**GPU throughput for the in-process runtime is still unmeasured**, as ADR-0022
left it, and now for a second reason: the 4090 is holding two vLLM engines, so
the probe would disturb exactly the service the `openai:` runtime depends on.
