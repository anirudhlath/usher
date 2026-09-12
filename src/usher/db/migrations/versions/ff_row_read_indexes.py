"""The read surface for M7's nine row providers."""

import sqlalchemy as sa
from alembic import op

revision = "ff"
down_revision = "fe1d40c8b7a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_watch_states_user_played", table_name="watch_states")
    op.create_index(
        "ix_watch_states_user_recent",
        "watch_states",
        ["user_id", "played", sa.text("last_played_at DESC NULLS LAST")],
        unique=False,
    )
    op.create_index(
        "ix_media_items_recently_added",
        "media_items",
        [sa.text("added_at DESC NULLS LAST")],
        unique=False,
        postgresql_where=sa.text("available AND title_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_media_items_recently_added", table_name="media_items")
    op.drop_index("ix_watch_states_user_recent", table_name="watch_states")
    op.create_index("ix_watch_states_user_played", "watch_states", ["user_id", "played"])
