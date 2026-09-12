"""`title_neighbors.computed_at` gets an index.

Revision ID: m10d
Revises: m10c
Create Date: 2026-09-12

`NeighborRebuildJob.last_done()` is `min(computed_at)` and the scheduler asks it
once a tick, so without this the read scans the whole table to learn a job is
not due. `downgrade()` is load-bearing: `title_neighbors` outlives this revision
and nothing else drops the index.

## Cost

**`CREATE INDEX CONCURRENTLY` cannot run here**, because `env.py` wraps both
migration modes in `context.begin_transaction()` -- `ff_row_read_indexes.py`'s
finding, and it does not resolve trivially here either: `title_neighbors` is
populated on any deployment that has ever run the rebuild, so `CREATE INDEX`
takes a SHARE lock that blocks writes to it for the length of the build. The
only writer is the neighbour rebuild itself. An operator who cannot take that
window builds the index by hand with `CREATE INDEX CONCURRENTLY` and then
`alembic stamp`s this revision; that is an operator escape rather than the
default, and it is recorded here rather than discovered at 3am.
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
