"""`sync_runs` records the failure kind in a column, not as a message prefix.

Revision ID: m10d
Revises: m10c
Create Date: 2026-09-12

Nullable and staying nullable: a run that completed has no failure kind, and a
sentinel meaning "not applicable" would be a third member of a vocabulary whose
only job is telling two failures apart.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10d"
down_revision: str | Sequence[str] | None = "m10c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `m09d`'s spelling, minus the backfill-then-NOT-NULL half this column does
    # not want: never `server_default`, which would outlive this migration and
    # supply a plausible wrong value to a writer that forgot.
    op.add_column("sync_runs", sa.Column("error_code", sa.String(length=32), nullable=True))

    # Rows written before this revision carry their kind as the first token of
    # `error`, which is what `usher sync` matched on. Backfilled rather than
    # left NULL, or the command stops offering `--allow-full-retraction` for
    # every refusal already on disk.
    #
    # `error` itself is left alone: rewriting it would make `downgrade()` lossy
    # for rows this revision did not write, and the duplicated token is
    # cosmetic on a column nothing classifies by any more.
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
    # recorded at `m10d` loses its kind entirely rather than falling back to
    # the token `upgrade()` reads.
    op.drop_column("sync_runs", "error_code")
