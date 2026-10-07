"""Sync correctness: a unit's checkpoint, a walk's planned flag, a source merge's own instant."""

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
    op.execute(
        """
        CREATE FUNCTION watch_states_set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.origin IS DISTINCT FROM 'source' THEN
                NEW.updated_at = now();
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER trg_watch_states_set_updated_at ON watch_states")
    op.execute(
        "CREATE TRIGGER trg_watch_states_set_updated_at BEFORE UPDATE ON watch_states "
        "FOR EACH ROW EXECUTE FUNCTION watch_states_set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_watch_states_set_updated_at ON watch_states")
    op.execute(
        "CREATE TRIGGER trg_watch_states_set_updated_at BEFORE UPDATE ON watch_states "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute("DROP FUNCTION watch_states_set_updated_at()")
    op.drop_column("sync_runs", "planned")
    op.drop_column("sync_run_units", "checkpoint")
