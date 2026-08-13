"""Taste-side derived state: the MovieLens tag genome, and the per-user taste
centroid.

All three are *derived* — recomputable from inputs the catalog already holds —
and all three therefore carry provenance rather than an `updated_at` trigger.
M7 Group F landed `genome_scores`; its Group G added `user_taste` to this
module and to the same revision (`ffa`), which is what that migration's own
docstring instructs. **M8 Task 19 added `genome_tags` here** and to `m08b`.

**`genome_tags` is in this module rather than in `db/models/curation.py`,
which is where the M8 plan's file map put it.** The plan wrote it beside
`curated_rows`/`llm_calls` because M8 is the milestone that wants the words;
but this table is written by `bootstrap --phase movielens`, not by a
generation, and the one invariant it has is that its `genome_revision` equals
`genome_scores.genome_revision` — a column eleven lines down in this file.
A vocabulary filed under curation would put the two halves of that comparison
in two modules, and `curation.py`'s own docstring calls its pair "what a
generation produced, and its cost", which a bootstrap artefact is not.
Recorded in the M8 plan's amendment section.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.ports.bulk import GENOME_TAG_COUNT


class GenomeScoreRow(Base):
    """One title's MovieLens tag-genome vector.

    **One dense `halfvec(1128)` per title, not a tall `(title_id, tag_id,
    relevance)`** — boundary call 7, and the argument is a measurement.
    Priced on a scratch `pgvector/pgvector:pg17` (pgvector 0.8.6) at the real
    dimensions, 16,376 rows:

    | form | rows | total size |
    |---|---|---|
    | `halfvec(1128)`, one row per title | 16,376 | **45 MB** |
    | `real[]`, one row per title | 16,376 | 88 MB |
    | `(title_id, tag_id smallint, relevance real)` | **18,472,128** | **2,106 MB** |

    **47x**, against a database PRD 08 budgets at 8-12 GB *total*. `real[]`
    sits between and is worse than both: no operator class, so the similarity
    term stops being a single `<=>` and becomes arithmetic in Python. The
    genome is a genuinely dense matrix — every one of 16,376 movies carries a
    value for every one of 1,128 tags, verified by counting — so the tall form
    stores 16,376 copies of the tag id and the title id to express a matrix
    with no holes in it.

    **`NOT NULL` on the vector, unlike `title_embeddings.embedding`, and the
    contrast is written down here so nobody "fixes" one to match the other.**
    That column is nullable for a specific reason: a *refusal* is a written
    outcome, so a title whose composed document is degenerate gets a row with
    a NULL vector and stops matching the stale predicate. There is no
    analogous outcome here. The dataset never produces an empty or degenerate
    vector — a run of the wrong length is `PortDataMalformed`, not a row — so
    the only two states are "has a genome row" and "does not", and the
    *absence of the row* is the signal. ADR-0014: absence is not zero, and
    here absence is spelled by there being nothing to read.

    **`genome_revision` is ADR-0020's shape, and it is the one real design
    question this table had.** The tag vocabulary can change between
    releases, so a vector is only comparable to another computed from the
    same 1,128 tags in the same order. Two vectors from different releases
    have the same type, the same width, and nothing else to tell them apart;
    a half-migrated table is indistinguishable from a consistent one and
    every cosine over it is quietly meaningless. So each row carries the
    dataset revision that produced it — the archive ETag, measured
    `"14ea425b-600f0e149d407"` — and `GenomeRepository.get_pair` returns
    `None` across a mismatch. Three alternatives, each rejected for a stated
    reason:

    - *A hash of the ordered tag id list* names the thing that actually
      matters rather than the archive that carried it, which is better in
      principle. Rejected because interpreting it requires storing or
      recomputing the vocabulary, while the archive revision is already the
      value `ImportRun.revision` holds for this dataset — so the two agree by
      construction and a mismatch is one comparison rather than a second
      derivation to keep in step. If a later milestone needs to compare
      across releases, the hash is the upgrade.
    - *A one-row metadata table naming the current revision* cannot express a
      half-migrated table, which is precisely the state a killed re-import
      leaves and precisely what this column is for.
    - *No column at all* is what makes the failure silent. The honest
      counterweight: `ml-latest` has not moved since 2023-07-20, so in
      practice every row will carry one value and this column will look like
      dead weight for years. Its job is to make the day that stops being true
      visible instead of silent, and 16,376 short strings is the price.

    **The tag vocabulary was deliberately not stored in M7, and M8 Task 19
    stored it — `GenomeTagRow` below.** M7's argument, kept because it is why
    the table did not ship a milestone earlier: nothing in M7 reads a tag
    *name*, since cosine over two vectors needs the two vectors and the
    guarantee that their positions mean the same thing, not the knowledge that
    position 431 is a word; `genome-tags.csv` was read by the importer to
    verify contiguity and width, and thrown away. The cost landed exactly
    where it was predicted to — **an LLM prompt that wants to say
    "atmospheric, thought-provoking" needs the words** — and it was paid the
    way this paragraph said it would be: a 1,128-row table plus a loader step
    in a phase that already reads the file, and one migration (`m08b`).
    **What made that safe rather than a deferral-by-omission is this very
    column**, which is what `genome_tags.genome_revision` is compared against.
    ("Tiny" was not a reason to build the table early; `search_queries` is the
    standing example of a table built ahead of its writer.)

    **No HNSW index, and this is a decision rather than an omission** — and
    the numbers are measured against the shipped
    access pattern rather than against a full pairwise scan:

    - **`get_pair`, the only read this table has, is 0.062 ms.** Two primary
      key probes under a `BitmapOr`, 2 buffers each. An HNSW index cannot
      help a lookup *by* `title_id`; it answers a question nobody puts to
      this table.
    - **A KNN — one seed against all 15,565 stored vectors, `Seq Scan`, no
      index — is 59.4-66.2 ms over three warm runs**, at 93,617 buffers,
      which is dominated by the TOAST fetch per row. Nothing asks for that
      today. If something ever does, this decision genuinely reopens, and
      that is the honest statement rather than a number that forecloses it.
    - **M6 measured what an index the planner *prefers* costs**: a GiST index
      alongside GIN turned 33.3 ms into 141.5 ms p50, 4.3x, for
      byte-identical recall. An index nothing reads is `ix_titles_popularity`
      again.

    **The plan's "a full pairwise cosine over every one of the 16,376 vectors
    runs in 1.190 ms" is not any of those, and it is not achievable.** A full
    pairwise scan is 121M unordered pairs of 1,128 lanes; measured here as a
    self-join it is **384 s**. 1.190 ms is roughly the cost of *one* pair.
    Corrected rather than repeated, because the "no index" decision rests on
    it and a reader checking the number would have found it off by five
    orders of magnitude — the decision survives on the first bullet above,
    which is the one that was always doing the work.

    The 45 MB figure already includes **624 kB of index**: that is the
    primary key on `title_id`, which every read uses. No other index ships,
    and `tests/integration/test_genome_repository.py` asserts it.

    **The vector is TOASTed, and that is worth knowing before someone
    measures it again.** 1,128 halfvec lanes is 2,256 bytes plus a header,
    past Postgres's ~2 kB inline threshold, so the heap holds 1,096 kB of
    pointers and the TOAST relation holds 43 MB. Every read of a genome
    vector pays a TOAST fetch. At 16,376 rows and one `<=>` per candidate
    pair that is invisible; it would not be at 1.27M rows, which is one more
    reason the population here is the genome's own 16,376 rather than the
    catalog's.

    **The stored relevances are the archive's own values, untransformed.**
    Measured over all 268,157,000 off-diagonal pairs against a bar written
    before the run: mean 0.6101, sd 0.0913, p1 0.4075, top-10 gap 0.2456 — it
    does not saturate. `usher.adapters.bulk.movielens` carries that table and
    the two mean-centred variants that were measured alongside it. Note that
    the corpus mean is recoverable from *this table*, because the stored
    population is the whole corpus, so a read-side centring needs no
    re-import and no extra column. **The spelling matters and was verified:**
    `avg(relevance)` fails (`halfvec` has no `avg` usable this way and does
    not support subscripting), while
    `SELECT (avg(relevance::vector)::real[]) FROM genome_scores` returns mu
    lane by lane. `relevance::real[]` extracts a single row's lanes the same
    way.
    """

    __tablename__ = "genome_scores"

    # The primary key *is* the foreign key, exactly as `title_embeddings`:
    # one vector per title, and a surrogate id would add a column nothing
    # reads while permitting two rows per title -- a state no consumer could
    # interpret.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    relevance: Mapped[list[float]] = mapped_column(HALFVEC(GENOME_TAG_COUNT), nullable=False)
    genome_revision: Mapped[str] = mapped_column(Text, nullable=False)
    # `computed_at` and no `updated_at`, and no trigger -- following
    # `title_neighbors`, which has none either because "a neighbour row is a
    # batch artefact: it is computed, wholesale, by one pass, and
    # `computed_at` is the only timestamp that means anything about it."
    # Identical here. This is also mechanical:
    # `tests/integration/test_migrations.py::
    # test_migration_creates_the_updated_at_triggers` asserts the trigger set
    # *exactly*, so this table must add none and that test must not change.
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GenomeTagRow(Base):
    """What each of `genome_scores.relevance`'s 1,128 lanes means.

    **Three columns, and the third is the whole reason the table is safe.**
    `genome_scores` stores a `halfvec(1128)` whose positions are meaningful
    only relative to a vocabulary; this is that vocabulary. Two releases'
    vocabularies are the same type, the same width and, at 1,128 rows of
    English words, indistinguishable by eye — so a vocabulary loaded from one
    archive and a vector loaded from another produce labels that are wrong and
    plausible, in prose, on a screen. `genome_revision` is what makes that a
    comparison rather than a hope, and `GenomeRepository.vocabulary` refuses
    across a mismatch. It is ADR-0020 again, and `GenomeScoreRow`'s own
    `genome_revision` paragraph is where the shape is argued.

    **`tag_id` is `Integer`, and the argument is not size — 1,128 rows is
    2.2 kB either way.** It is which layer refuses an out-of-range value.
    `.claude/rules/db-and-sql.md`'s standing trap is a column *narrower* than
    the field feeding it: such a value is refused by asyncpg's own binary
    encoder, client-side, as an unnamed `DataError` (SQLSTATE `22000`, the
    measured `curated_rows."position"` instance), and through a `COPY` it is a
    bare `builtins.OverflowError` with no SQLSTATE at all. Two decisions keep
    this column out of that:

    - **The ceiling is a batch precondition, not a field bound.**
      `BulkCatalogRepository.replace_genome_tags` refuses a vocabulary that is
      not exactly `1…n` before it writes anything, so the largest `tag_id`
      that can reach the driver is the length of the sequence handed in. That
      also catches the failure a per-row `le=` cannot see — a *gap*, which
      renames every later lane.
    - **`Integer` puts the remaining boundary out of reach.** Under
      `SmallInteger` a caller reaches the encoder with a 32,768-element list;
      under `Integer` it would need `2**31` elements. Everything below that is
      refused by `ck_genome_tags_tag_id_in_vocabulary` — an `IntegrityError`
      carrying a constraint name, which is the classifiable path
      `is_row_refusal` and `constraint_name` were consolidated for. Measured
      on `pgvector/pgvector:pg17`, 2026-08-07: at **32,768** the two types
      diverge, `integer` answering `IntegrityError` SQLSTATE `23514` naming
      the constraint and `smallint` answering `DBAPIError` SQLSTATE `22000`
      naming nothing. `m08b`'s docstring holds the whole table.

    **No index beyond the primary key.** `genome_scores`' and `llm_calls`'
    precedent: the only read this table has is the whole of it, in `tag_id`
    order, which the primary key already serves. An index nothing reads is
    `ix_titles_popularity` again.

    **No `computed_at`, unlike `genome_scores` one class up**, and the
    asymmetry is real rather than an oversight. A genome vector's age is not
    recoverable from `import_runs`, because a resumed import writes different
    movies in different runs; this table is written by one statement pair in
    one transaction, so its age *is* `import_runs` for `movielens.genome` —
    the row an operator already reads with `usher bootstrap-status`. A fourth
    column would be a second, drift-capable copy of that timestamp.
    """

    __tablename__ = "genome_tags"

    # MovieLens' own lane index, never minted here -- `autoincrement=False` so
    # a `tag_id` this project invented is not even expressible. The primary
    # key *is* the natural key, exactly as `genome_scores.title_id` is: a
    # surrogate id would add a column nothing reads while permitting two rows
    # for one lane, which is a state no consumer could interpret.
    tag_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    tag: Mapped[str] = mapped_column(Text, nullable=False)
    genome_revision: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # `1…GENOME_TAG_COUNT`, the same constant `genome_scores.relevance`
        # declares its width with, so the two cannot drift. The lower bound is
        # as load-bearing as the upper: `tag_id - 1` is a list index, and `0`
        # would make lane -1 addressable.
        CheckConstraint(
            f"tag_id BETWEEN 1 AND {GENOME_TAG_COUNT}",
            name="ck_genome_tags_tag_id_in_vocabulary",
        ),
        # An empty name is a lane that reads as labelled and says nothing --
        # worse than a missing row, which the contiguity check would catch.
        CheckConstraint("tag <> ''", name="ck_genome_tags_tag_not_empty"),
        # An empty revision matches no `genome_scores` row, so every read of
        # the vocabulary would refuse and the table would be silently inert.
        CheckConstraint("genome_revision <> ''", name="ck_genome_tags_revision_not_empty"),
    )


class UserTasteRow(Base):
    """One user's taste centroid, and the fingerprint that invalidates it.

    **The invalidation is a predicate, not an event, and that is trap 5.** PRD
    06's caching table says the centroid is *"invalidated on watch-state
    change"*. The nightly walk merges up to 1,126,789 watch states; one
    invalidation per merged row is the fan-out PRD 07 explicitly refuses for
    `watchstate.updated`, spent on at most one useful recomputation per user
    per night. So the merge path publishes nothing, writes nothing here, and
    does not know this table exists. `source_watermark` carries the
    `max(watch_states.updated_at)` the row was computed from and
    `TasteService` recomputes when the household's current max differs --
    ADR-0020's scheme, per user rather than per title.

    **`centroid` is nullable and that is load-bearing**, for
    `title_embeddings.embedding`'s reason one module over. A household below
    the minimum engaged-title count is a **written refusal**, not a missing
    row. Without somewhere to write it, a four-title household is recomputed on
    every read of every home screen forever, and the fifth title does not
    re-claim the centroid once -- it re-claims it always. This project has
    shipped that exact bug before, in the watch-history repair.

    **`source_watermark` is nullable, and the plan specified `NOT NULL`.** The
    aggregate it stores is `max()` over a possibly-empty set, so a household
    that has watched nothing has no honest value for it. Under `NOT NULL` the
    refusal for that household cannot be written at all, which makes it the one
    household recomputed on every single read -- the bug the nullable
    `centroid` column two lines up exists to prevent, arriving through the
    other column. Nullable also makes the predicate correct by itself: `NULL IS
    DISTINCT FROM NULL` is false, so a stored empty-history refusal reads as
    current, and the first watch state that lands moves the max off `NULL` and
    re-claims it exactly once.

    **`model_name` records runtime *and* checkpoint** (`fastembed:BAAI/
    bge-small-en-v1.5`), exactly as `title_embeddings` does. The fastembed and
    sentence-transformers vectors for one checkpoint differ by up to 1.41e-03
    in pairwise similarity, so the two are not interchangeable; putting the
    runtime in the name means a swap invalidates every centroid through the
    same `IS DISTINCT FROM` clause rather than through a migration somebody
    has to remember to write.

    **`halfvec(1024)`, matching `title_embeddings.embedding` exactly** --
    both are `EMBEDDING_DIMENSIONS`, and `m09e` moved them together for that
    reason.
    ** A
    centroid is a mean of vectors from that column and is compared against it,
    so a different type or width would make the one comparison this table
    exists for a cast. The measured `halfvec` round-trip cost is a max cosine
    error of 1.21e-04, three orders of magnitude below the useful signal --
    which is also why `tests/unit/test_services_taste.py` asserts to 1e-9 and
    the integration file to 1e-3.

    **No `updated_at` and no trigger.** One writer, one statement, which sets
    `computed_at` in its own `ON CONFLICT DO UPDATE` -- `title_embeddings`'
    precedent, and mechanically required:
    `tests/integration/test_migrations.py::
    test_migration_creates_the_updated_at_triggers` asserts the trigger set
    *exactly*, so a trigger here is a failing case elsewhere.
    """

    __tablename__ = "user_taste"

    # The primary key *is* the foreign key, exactly as
    # `title_embeddings.title_id` and `genome_scores.title_id` are. One
    # centroid per user; a surrogate id would permit two rows for one user,
    # which is a state no consumer could interpret.
    #
    # CASCADE because a centroid protects no user state and is fully
    # re-derivable from `watch_states` and `title_embeddings`. Contrast
    # ADR-0010's RESTRICT on `watch_states.title_id`, which guards state a
    # delete would destroy irrecoverably.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    centroid: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS))
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many titles went into the mean. Makes the refusal *countable*: a
    # gauge reading "N households have too little history for a centroid" is
    # how an operator learns that half the deployment gets no taste rows,
    # which is otherwise indistinguishable from taste rows nobody clicked.
    title_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The artefact's age, for the operator -- and deliberately *not* the
    # invalidation. A TTL here would recompute a centroid whose inputs have
    # not moved and serve a stale one whose inputs have.
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
