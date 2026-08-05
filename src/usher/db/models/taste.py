"""Taste-side derived state: the MovieLens tag genome, and (from group G)
the per-user taste centroid.

Both are *derived* — recomputable from inputs the catalog already holds — and
both therefore carry provenance rather than an `updated_at` trigger. This
module holds `genome_scores`; `user_taste` joins it in the same revision.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
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

    **The tag vocabulary itself is deliberately not stored, and the cost is
    recorded here rather than discovered later.** Nothing in M7 reads a tag
    *name*: cosine over two vectors needs the two vectors and the guarantee
    that their positions mean the same thing, not the knowledge that position
    431 is a word. `genome-tags.csv` is read by the importer to verify
    contiguity and width, and thrown away. The real cost is M8's: **an LLM
    prompt that wants to say "atmospheric, thought-provoking" needs the
    words**, and a vector alone cannot produce them. So M8 must either
    re-read `genome-tags.csv` (18,103 bytes out of an archive it would
    otherwise not need) or add the table then — a 1,128-row table plus a
    loader step in a phase that already reads the file, and one migration.
    What makes that safe rather than a deferral-by-omission is this very
    column: the vocabulary M8 loads must carry the same revision as the
    vectors it explains, and there is already something to check it against.
    ("Tiny" is not a reason to build the table now; `search_queries` is the
    standing example of a table built ahead of its writer.)

    **No HNSW index, and this is a decision rather than an omission.** Three
    reasons, none of which is "it would be slow to build":

    - **16,376 rows never needs one.** A full pairwise cosine over every one
      of them — `Seq Scan`, no index — is 1.190 ms, measured.
    - **The access pattern is a pair lookup by `title_id`, not a KNN.** A
      similarity blend scores a candidate pair it already holds; nothing asks
      this table for its nearest neighbours. An index answers a question
      nobody puts to it.
    - **M6 measured what an index the planner *prefers* costs.** A GiST index
      alongside GIN turned 33.3 ms into 141.5 ms p50 — 4.3x — for
      byte-identical recall, because the planner reached for it. An HNSW
      index on a 16k-row table is the same shape of harm with the same cause.
      And an index nothing reads is `ix_titles_popularity` again.

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
    re-import and no extra column.
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
