"""The MovieLens tag genome as one dense halfvec per title, and the per-user
taste centroid.

Revision ID: ffa
Revises: ff
Create Date: 2026-08-04

**Two tables, added by two groups to one revision.** Group F created this file
with `genome_scores`; Group G added `user_taste` to it rather than taking
`ffb`, which is what the paragraph near the bottom of this docstring told it to
do and what keeps the milestone's migration budget at four. Both are derived
state carrying a fingerprint, both live in `db/models/taste.py`, and the branch
is unreleased -- so amending an unmerged revision is legal. A developer
database that has already applied the `genome_scores`-only form recovers with
`alembic downgrade -1` then `alembic upgrade head`.

**`user_taste` is the fingerprint half, and it is where trap 5 is refused.**
PRD 06's caching table says the centroid is *"invalidated on watch-state
change"*. The nightly walk merges up to 1,126,789 watch states, so an
invalidation per merged row is the same fan-out PRD 07 declines to publish for
`watchstate.updated` -- a million events a night for at most one useful
recomputation per user. `source_watermark` holds the
`max(watch_states.updated_at)` the centroid was computed from and
`TasteService` recomputes on a demand read when the household's current max
differs. ADR-0020, per user rather than per title. Two of its columns are
nullable on purpose and both are load-bearing:

- **`centroid`**, so a household below the minimum engaged-title count is a
  *written refusal* rather than a missing row -- `title_embeddings.embedding`'s
  exact argument. Without it that household is recomputed on every read of
  every home screen forever.
- **`source_watermark`**, because `max()` over an empty history is `NULL` and
  there is no honest value to write for a household that has watched nothing.
  `NOT NULL` there (which the plan specified) makes that household the one
  whose refusal cannot be stored, i.e. reintroduces the bug the nullable
  `centroid` prevents, through the other column. Nullable also makes the
  predicate self-consistent: `NULL IS DISTINCT FROM NULL` is false, so the
  stored refusal reads as current until the first watch state lands.

**`user_taste` adds no `updated_at` and no trigger.** One writer, one
statement, setting `computed_at` in its own `ON CONFLICT DO UPDATE` --
`title_embeddings`' precedent, and mechanically required, because
`test_migration_creates_the_updated_at_triggers` asserts the trigger set
exactly.

**One table, `genome_scores`, and boundary call 7 is the whole of it.** PRD
02 implies a tall `(title_id, tag_id, relevance)` shape; this is where that
is refused, with the measurement rather than the argument. Priced on a
scratch `pgvector/pgvector:pg17` (pgvector 0.8.6) at the real dimensions:

    form                                          rows   total size
    ------------------------------------  ------------  -----------
    halfvec(1128), one row per title             16,376        45 MB
    real[], one row per title                    16,376        88 MB
    (title_id, tag_id smallint, relevance)   18,472,128     2,106 MB

**47x**, against a database PRD 08 budgets at 8-12 GB *total*. The 45 MB
splits 1,096 kB heap + 43 MB TOAST + 624 kB index, and that index is this
table's primary key -- **no other index ships**, deliberately. Measured
against a real 15,565-row load rather than against a scan nobody runs:
`get_pair`, the only read this table has, is **0.062 ms** (two primary-key
probes under a `BitmapOr`), and an HNSW index cannot help a lookup *by*
`title_id`. A KNN -- one seed against all 15,565, `Seq Scan`, no index -- is
**59.4-66.2 ms** at 93,617 buffers, dominated by one TOAST fetch per row;
nothing asks for that today, and if something ever does this decision
reopens honestly rather than being foreclosed. M6 separately measured a
planner-*preferred* index costing 4.3x for byte-identical recall.
`tests/integration/test_genome_repository.py` asserts the index set so a
later migration cannot quietly add an HNSW one "for similarity".

**The plan's "1.190 ms for a full pairwise cosine" is corrected here rather
than repeated.** A full pairwise scan is 121M pairs of 1,128 lanes and
measures at **384 s** as a self-join; 1.190 ms is about the cost of one
pair. The decision survives on the access pattern, which is what was always
carrying it.

**This migration creates no extension.** `fa2b6c1e9d30` already creates
`vector` (plus `pg_trgm` and `fuzzystrmatch`) `IF NOT EXISTS` and never drops
them. That migration's docstring records the asymmetry, and it now applies to
a **second** dependent column: `DROP EXTENSION vector` fails while any object
depends on it, and after this migration that includes
`genome_scores.relevance` as well as `title_embeddings.embedding`. `DROP
EXTENSION vector CASCADE` is the version that "works", and it works by
silently dropping the dependent columns -- which is what ADR-0010 refuses one
layer down. Nothing about that changes here; the note simply gains a second
dependent, and this migration's own downgrade drops only its own table.

**The revision id extends by a character, and `ff` was the last two-character
one.** `fa2b6c1e9d30`'s convention fixes one hex character per migration;
M6's cycle ended at `fc`, M7 spent `fd`/`fe`/`ff`, and no hex character sorts
after `f`. So `ff` -> `ffa` -> `ffb`, which is unbounded and still sorts
correctly under a plain `ls` -- rather than a third cycle starting with a
digit, which would sort *before* `fa` and lose the only thing the convention
ever bought. Alembic orders by `down_revision` and never cared; `ls` order
matching chain order is what is being preserved.

**Group G amends this revision rather than adding a fifth.** `user_taste`
belongs in `db/models/taste.py` beside `genome_scores` and in this same
migration. The branch is unreleased and its head has not shipped, so amending
an unmerged revision is legal; if a developer database has already applied
it, `alembic downgrade -1` then `alembic upgrade head` is the recovery. Said
here so group G finds the instruction where it will be looking.

Reversible in both directions. Verified empty -> head -> `downgrade base` ->
head against a real `pgvector/pgvector:pg17`.
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.ports.bulk import GENOME_TAG_COUNT

revision = "ffa"
down_revision = "ff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "genome_scores",
        # The primary key *is* the foreign key, exactly as
        # `title_embeddings`: one vector per title, and a surrogate id would
        # add a column nothing reads while permitting two rows per title.
        #
        # CASCADE, and it is the `title_embeddings` case rather than the
        # `watch_states` one. ADR-0010 makes `watch_states.title_id` RESTRICT
        # because a watch state is user state a delete would destroy
        # silently; a genome vector is neither user state nor irrecoverable.
        # After a repointing merge the loser's vector describes a film that
        # is no longer the canonical title, so it dies with the loser rather
        # than blocking the delete or surviving attached to nothing.
        sa.Column(
            "title_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("titles.id", ondelete="CASCADE", name="fk_genome_scores_title_id_titles"),
            primary_key=True,
        ),
        # NOT NULL, unlike `title_embeddings.embedding`. That column is
        # nullable so a *refusal* has somewhere to be written; the genome has
        # no analogous outcome (a run of the wrong length is
        # `PortDataMalformed`, not a row), so the only two states are "has a
        # row" and "does not" and the absence of the row is the signal.
        sa.Column("relevance", HALFVEC(GENOME_TAG_COUNT), nullable=False),
        # ADR-0020: derived state carries its fingerprint. The tag vocabulary
        # can change between releases and two vectors from different releases
        # are type-identical and same-width, so a mixed table yields cosines
        # that are wrong and plausible. `GenomeRepository.get_pair` refuses
        # across a mismatch, and an operator counts one with
        # `SELECT genome_revision, count(*) FROM genome_scores GROUP BY 1`.
        sa.Column("genome_revision", sa.Text(), nullable=False),
        # `computed_at` and no `updated_at`, and no trigger: this follows
        # `title_neighbors`, where a row is a batch artefact computed
        # wholesale by one pass. `test_migration_creates_the_updated_at_
        # triggers` asserts the trigger set exactly, so this table adds none.
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("title_id", name="pk_genome_scores"),
    )
    op.create_table(
        "user_taste",
        # The primary key *is* the foreign key, exactly as
        # `title_embeddings.title_id` and `genome_scores.title_id` above. One
        # centroid per user; a surrogate id would permit two rows for one
        # user, a state no consumer could interpret.
        #
        # CASCADE, and it is the `title_embeddings` case rather than the
        # `watch_states` one. ADR-0010 makes `watch_states.user_id` protect
        # user state a delete would destroy irrecoverably; a centroid is
        # neither user state nor irrecoverable -- it is a mean over rows that
        # are themselves cascading away.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_taste_user_id_users"),
            primary_key=True,
        ),
        # Nullable, and this is `title_embeddings.embedding`'s argument
        # exactly: a household below the minimum engaged-title count is a
        # WRITTEN REFUSAL, not a missing row. A `NOT NULL` column has nowhere
        # to write that outcome, so such a household is recomputed on every
        # read of every home screen forever and the title that lifts it over
        # the minimum re-claims it always rather than once.
        sa.Column("centroid", HALFVEC(EMBEDDING_DIMENSIONS), nullable=True),
        # Runtime *and* checkpoint, per `Embedder.model_name`. A model swap
        # invalidates every centroid through `IS DISTINCT FROM :model_name`
        # rather than through a migration somebody has to remember to write.
        sa.Column("model_name", sa.Text(), nullable=False),
        # ADR-0020's fingerprint, and **nullable against the plan's NOT
        # NULL**. It stores `max(watch_states.updated_at)` for this user, an
        # aggregate over a possibly-empty set: a household that has watched
        # nothing has no honest value for it. Under `NOT NULL` that
        # household's refusal cannot be stored at all, which reintroduces
        # through this column the recompute-forever bug the nullable
        # `centroid` prevents. Nullable also makes the predicate correct on
        # its own terms -- `NULL IS DISTINCT FROM NULL` is false, so a stored
        # empty-history refusal reads as current, and the first watch state
        # to land moves the max off NULL and re-claims it exactly once.
        sa.Column("source_watermark", sa.DateTime(timezone=True), nullable=True),
        # Makes the refusal countable. A gauge reading "N households have too
        # little history for a centroid" is how an operator learns half the
        # deployment gets no taste rows -- otherwise indistinguishable from
        # taste rows nobody clicked.
        sa.Column("title_count", sa.Integer(), nullable=False),
        # The artefact's age, for the operator, and deliberately NOT the
        # invalidation. A TTL here would recompute a centroid whose inputs
        # have not moved and serve a stale one whose inputs have.
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_taste"),
    )


def downgrade() -> None:
    op.drop_table("user_taste")
    op.drop_table("genome_scores")
