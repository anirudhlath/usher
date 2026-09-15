"""The MovieLens tag genome as one dense halfvec per title, and the per-user taste centroid."""

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
        # The primary key *is* the foreign key, exactly as `title_embeddings`: one
        # vector per title, and a surrogate id would add a column nothing reads while
        # permitting two rows per title.
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
        # Derived state carries the fingerprint of its input.
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
        # The primary key *is* the foreign key, exactly as `title_embeddings.title_id`
        # and `genome_scores.title_id` above.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_taste_user_id_users"),
            primary_key=True,
        ),
        # Nullable, and this is `title_embeddings.embedding`'s argument exactly: a
        # household below the minimum engaged-title count is a WRITTEN REFUSAL, not a
        # missing row.
        sa.Column("centroid", HALFVEC(EMBEDDING_DIMENSIONS), nullable=True),
        # Runtime *and* checkpoint, per `Embedder.model_name`. A model swap
        # invalidates every centroid through `IS DISTINCT FROM :model_name`
        # rather than through a migration somebody has to remember to write.
        sa.Column("model_name", sa.Text(), nullable=False),
        # The input fingerprint, and **nullable**: a user with no history has no
        # watermark to record.
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
