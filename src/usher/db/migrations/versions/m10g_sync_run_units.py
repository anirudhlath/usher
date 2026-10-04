"""A whole-library walk's plan: `sync_run_units`, and `sync_runs.heartbeat_at`."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10g"
down_revision: str | Sequence[str] | None = "m10f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Never a server default: a run written before this revision has no heartbeat,
    # and that is what marks it as a walk without a plan.
    op.add_column("sync_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "sync_run_units",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("unit_key", sa.Text(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum("seed", "titles", "episodes", name="walkstage", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expected_items", sa.Integer(), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                name="syncrununitstatus",
                native_enum=False,
                length=16,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.CheckConstraint("unit_key <> ''", name="ck_sync_run_units_unit_key_not_empty"),
        sa.CheckConstraint('"position" >= 0', name="ck_sync_run_units_position_non_negative"),
        sa.CheckConstraint(
            "expected_items >= 0", name="ck_sync_run_units_expected_items_non_negative"
        ),
        sa.CheckConstraint("items_seen >= 0", name="ck_sync_run_units_items_seen_non_negative"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_runs.id"],
            name=op.f("fk_sync_run_units_run_id_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "unit_key", name=op.f("pk_sync_run_units")),
    )


def downgrade() -> None:
    op.drop_table("sync_run_units")
    op.drop_column("sync_runs", "heartbeat_at")
