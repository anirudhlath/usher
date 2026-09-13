"""Tmdb_id namespaced by kind."""

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
