"""An index for the availability sweep.

Revision ID: f1a7d3c9e824
Revises: e5b8f2c40d17
Create Date: 2026-07-31

For `PostgresMediaItemRepository.mark_unseen_unavailable`'s `UPDATE`, and
for that statement only. Measured against pgvector/pgvector:pg17 at
1,126,674 rows on one source with 200 stale -- the realistic nightly shape
-- by `scripts/measure_ingest.py --scale 1126674`:

    UPDATE media_items SET available = false
    WHERE source_id = :source_id AND available AND last_seen_at < :seen_since

goes from `Seq Scan` (`Rows Removed by Filter: 1,126,474`, 173 ms) to
`Index Scan using ix_media_items_sweep` with an `Index Cond` on all three
columns, 102 ms. Before this index the only one leading with `source_id` is
`uq_media_items_source_external`, which carries neither `available` nor
`last_seen_at`, so there is nothing for the predicate to seek with.

**It does not help the guard**, and the measurement says so rather than
implying otherwise: `count(*)` plus `count(*) FILTER (...)` scoped to one
source is a `Parallel Seq Scan` with the index (87 ms) and without it
(86 ms). ADR-0015's ceiling is a *fraction*, so the denominator is
unavoidable, and a source that *is* the whole table gives `source_id` no
selectivity. That statement is 87 ms at full library size, which is a cost
worth paying rather than a problem worth indexing around.

Reversible, and cheap in both directions: one index, no data movement.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a7d3c9e824"
down_revision: str | Sequence[str] | None = "e5b8f2c40d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_media_items_sweep",
        "media_items",
        ["source_id", "available", "last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_items_sweep", table_name="media_items")
