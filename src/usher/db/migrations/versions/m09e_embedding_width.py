"""The embedding width moves 384 -> 1024, and the fingerprint scheme cannot help."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "m09e"
down_revision: str | Sequence[str] | None = "m09d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# : The width this revision moves to and away from.
_NEW_WIDTH = 1024
_OLD_WIDTH = 384


def _resize(width: int) -> None:
    """Empty the three derived tables, then move both vector columns to `width`.

    One helper for both directions because the two are genuinely the same
    operation at a different number, and a hand-mirrored `downgrade()` is how
    a pair of these drift.
    """
    # Order is load-bearing twice.
    op.execute(sa.text("DELETE FROM title_neighbors"))
    op.execute(sa.text("DELETE FROM user_taste"))
    op.execute(sa.text("DELETE FROM title_embeddings"))

    # Dropped rather than left in place: an HNSW index cannot survive its
    # column's type changing, and rebuilding an empty one costs nothing.
    op.drop_index("ix_title_embeddings_hnsw", table_name="title_embeddings")

    op.alter_column(
        "title_embeddings",
        "embedding",
        existing_type=HALFVEC(_OLD_WIDTH if width == _NEW_WIDTH else _NEW_WIDTH),
        type_=HALFVEC(width),
        existing_nullable=True,
    )
    op.alter_column(
        "user_taste",
        "centroid",
        existing_type=HALFVEC(_OLD_WIDTH if width == _NEW_WIDTH else _NEW_WIDTH),
        type_=HALFVEC(width),
        existing_nullable=True,
    )

    # `fb4e0a7d2c15`'s parameters, unchanged and spelled out rather than unpacked from a
    # dict -- `op.create_index`'s keyword arguments are individually typed and
    # `**mapping` collapses them to one value type, which mypy refuses.
    op.create_index(
        "ix_title_embeddings_hnsw",
        "title_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def upgrade() -> None:
    _resize(_NEW_WIDTH)


def downgrade() -> None:
    _resize(_OLD_WIDTH)
