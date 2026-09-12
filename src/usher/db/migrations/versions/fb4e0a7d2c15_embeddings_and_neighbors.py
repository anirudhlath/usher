"""title_embeddings and title_neighbors, with the HNSW index"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "fb4e0a7d2c15"
down_revision: str | Sequence[str] | None = "fa2b6c1e9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "title_embeddings",
        sa.Column("title_id", sa.UUID(), nullable=False),
        # Nullable, and it is load-bearing: it is how a refused degenerate
        # document stops matching the stale predicate. See
        # db/models/search.py's class docstring.
        sa.Column("embedding", HALFVEC(384), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("model_name <> ''", name="ck_title_embeddings_model_name_not_empty"),
        sa.CheckConstraint(
            "source_fingerprint <> ''", name="ck_title_embeddings_fingerprint_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_title_embeddings_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("title_id", name=op.f("pk_title_embeddings")),
    )
    op.create_index(
        "ix_title_embeddings_hnsw",
        "title_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )
    op.create_table(
        "title_neighbors",
        sa.Column("title_id", sa.UUID(), nullable=False),
        sa.Column("neighbor_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("title_id <> neighbor_id", name="ck_title_neighbors_not_self"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_title_neighbors_score_range"),
        sa.CheckConstraint("rank >= 0", name="ck_title_neighbors_rank_non_negative"),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_title_neighbors_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["neighbor_id"],
            ["titles.id"],
            name=op.f("fk_title_neighbors_neighbor_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("title_id", "neighbor_id", name="pk_title_neighbors"),
    )
    op.create_index("ix_title_neighbors_neighbor_id", "title_neighbors", ["neighbor_id"])


def downgrade() -> None:
    op.drop_index("ix_title_neighbors_neighbor_id", table_name="title_neighbors")
    op.drop_table("title_neighbors")
    op.drop_index("ix_title_embeddings_hnsw", table_name="title_embeddings")
    op.drop_table("title_embeddings")
