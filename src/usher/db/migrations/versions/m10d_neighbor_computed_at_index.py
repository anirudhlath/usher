"""`title_neighbors.computed_at` gets an index.

Revision ID: m10d
Revises: m10c
Create Date: 2026-09-12

`NeighborRebuildJob.last_done()` is `min(computed_at)` and the scheduler asks it
once a tick, so without this the read scans the whole table to learn a job is
not due. `downgrade()` is load-bearing: `title_neighbors` outlives this revision
and nothing else drops the index.
"""

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
