"""The watch lane's walk resumes from a StartIndex checkpoint.

Revision ID: m10b
Revises: m10a
Create Date: 2026-08-25

`WatchStateSyncService` read its cursor from the newest *completed*
`watch_state` run, so on a deployment where none had completed the walk was
cursorless -- the whole library, ~5,688 pages against the household this
project measures. Any transient failure recorded `FAILED`, which left no
completed run, which left no cursor, which restarted the walk. It never once
succeeded (issue #41).

This column is the resume point: the `StartIndex` the walk reached, advanced
per **committed** batch, so a crash costs the batch in flight rather than the
run. `NOT NULL DEFAULT 0` because every existing row describes a walk that
either finished or will be restarted from the top, and 0 is that.

**Only the `watch_state` lane advances it.** `FULL` and `DELTA` have a working
cursor and leave it at 0; ADR-0042 carries the argument, including why the
resume point is a page position rather than a `since` timestamp (the yielded
record carries no such field, the walk is not ordered by one, and the field is
mutable).

## Cost

Catalog-only apart from the CHECK's validation scan. `ADD COLUMN` with a
non-volatile default has not rewritten a table since PostgreSQL 11 -- the
backfill value is stored in `pg_attribute.attmissingval` (with
`atthasmissing` set) and materialised on the next write of each row, while
the default *expression* lives in `pg_attrdef`. The CHECK scans `sync_runs`
under `ACCESS EXCLUSIVE` and is trivially satisfied, that table holding a
handful of rows per source per day rather than a catalog.
"""

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
