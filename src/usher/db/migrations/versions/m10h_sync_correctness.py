"""Sync correctness: a unit's resume checkpoint and a whole-library walk's flag."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10h"
down_revision: str | Sequence[str] | None = "m10g"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_run_units", sa.Column("checkpoint", sa.Text(), nullable=True))
    op.add_column(
        "sync_runs",
        sa.Column("planned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Until now a heartbeat on an item lane meant a planned walk.
    op.execute(
        "UPDATE sync_runs SET planned = true "
        "WHERE kind IN ('full', 'delta') AND heartbeat_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("sync_runs", "planned")
    op.drop_column("sync_run_units", "checkpoint")
