"""`sync_runs` records the failure kind in a column, not as a message prefix."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10f"
down_revision: str | Sequence[str] | None = "m10e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `m09d`'s spelling, minus the backfill-then-NOT-NULL half this column does
    # not want: never `server_default`, which would outlive this migration and
    # supply a plausible wrong value to a writer that forgot.
    op.add_column("sync_runs", sa.Column("error_code", sa.String(length=32), nullable=True))

    # The kind of a row written before this revision is the first token of its
    # `error`; backfilled, or `usher sync` stops offering
    # `--allow-full-retraction` for the refusals already on disk. `error`
    # itself is left alone, which is what keeps `downgrade()` non-lossy for
    # exactly those rows.
    op.execute(
        "UPDATE sync_runs SET error_code = 'availability_ceiling' "
        "WHERE starts_with(error, 'availability_ceiling:')"
    )
    op.execute(
        "UPDATE sync_runs SET error_code = 'gap_delta_ceiling' "
        "WHERE starts_with(error, 'gap_delta_ceiling:')"
    )


def downgrade() -> None:
    # Reversible in schema and lossy in data, for rows written *above* this
    # revision only: `ReconcileService` stops prefixing `error` here, so a run
    # recorded at `m10f` loses its kind entirely rather than falling back to
    # the token `upgrade()` reads.
    op.drop_column("sync_runs", "error_code")
