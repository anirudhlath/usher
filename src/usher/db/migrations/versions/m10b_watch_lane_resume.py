"""The watch lane's walk resumes from a StartIndex checkpoint."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10b"
down_revision: str | Sequence[str] | None = "m10a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("ck_sync_runs_position_non_negative", "sync_runs", '"position" >= 0')


def downgrade() -> None:
    op.drop_constraint("ck_sync_runs_position_non_negative", "sync_runs", type_="check")
    op.drop_column("sync_runs", "position")
