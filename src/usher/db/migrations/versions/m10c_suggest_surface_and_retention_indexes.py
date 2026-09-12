"""`search_queries` learns which surface asked, and three indexes land."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10c"
down_revision: str | Sequence[str] | None = "m10b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Three statements, and the order is the point: nullable, backfilled, then NOT NULL.
    op.add_column("search_queries", sa.Column("surface", sa.String(length=8), nullable=True))
    op.execute("UPDATE search_queries SET surface = 'search'")
    op.alter_column("search_queries", "surface", nullable=False)

    # Nullable and staying nullable: a `search` row has no tier.
    op.add_column("search_queries", sa.Column("tier", sa.String(length=6), nullable=True))

    op.create_index("ix_search_queries_at", "search_queries", ["at"])

    # Both quoted from `m08a_curation.py`'s docstring rather than re-derived.
    op.create_index("ix_llm_calls_at", "llm_calls", ["at"])
    # -- dashboard 5's "cost per curated row", joining curated_rows on -- generation_id.
    op.create_index(
        "ix_llm_calls_generation_id",
        "llm_calls",
        ["generation_id"],
        postgresql_where=sa.text("generation_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Load-bearing, all three -- see the module docstring. Nothing else drops
    # them, and both tables outlive this revision.
    op.drop_index("ix_llm_calls_generation_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_at", table_name="llm_calls")
    op.drop_index("ix_search_queries_at", table_name="search_queries")

    op.drop_column("search_queries", "tier")
    op.drop_column("search_queries", "surface")
