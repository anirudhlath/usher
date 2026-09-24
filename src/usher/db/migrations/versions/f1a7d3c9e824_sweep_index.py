"""An index for the availability sweep."""

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
