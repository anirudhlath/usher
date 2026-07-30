"""tmdb_id namespaced by kind

Revision ID: b3f1c07d4a92
Revises: a8a0e10ff464
Create Date: 2026-07-30

Replaces the single-column unique index on titles.tmdb_id with a composite
one over (tmdb_id, kind). See ADR-0011: TMDb's movie and series id spaces
overlap on 26,968 of 56,975 distinct series ids (measured 2026-07-30), so
the old index blocked 47.3% of television from ever carrying a tmdb_id.

Fully reversible. The downgrade can fail on a database that already holds a
movie and a series sharing one tmdb_id -- correctly: those rows are exactly
what the narrower index cannot represent, and failing loudly beats
discarding one of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1c07d4a92"
down_revision: str | Sequence[str] | None = "a8a0e10ff464"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(
        "ix_titles_tmdb_id",
        table_name="titles",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_index(
        "ix_titles_tmdb_id_kind",
        "titles",
        ["tmdb_id", "kind"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_titles_tmdb_id_kind",
        table_name="titles",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_index(
        "ix_titles_tmdb_id",
        "titles",
        ["tmdb_id"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
