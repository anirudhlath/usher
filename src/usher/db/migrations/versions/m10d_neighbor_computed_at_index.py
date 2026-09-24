"""`title_neighbors.computed_at` gets an index."""

from collections.abc import Sequence

from alembic import op

revision: str = "m10d"
down_revision: str | Sequence[str] | None = "m10c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_title_neighbors_computed_at", "title_neighbors", ["computed_at"])


def downgrade() -> None:
    op.drop_index("ix_title_neighbors_computed_at", table_name="title_neighbors")
