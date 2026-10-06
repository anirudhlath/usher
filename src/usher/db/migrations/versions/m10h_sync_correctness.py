"""Sync correctness: a unit's resume checkpoint."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10h"
down_revision: str | Sequence[str] | None = "m10g"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_run_units", sa.Column("checkpoint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_run_units", "checkpoint")
