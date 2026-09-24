"""`images` gets its natural key: the owner triple plus the two provider columns."""

from alembic import op

revision = "m09c"
down_revision = "m09a"
branch_labels = None
depends_on = None

#: The owner triple plus the two provider columns, in one place, because
#: `upgrade()` names it and `downgrade()` must not name a different one.
_KEY_COLUMNS = ("title_id", "episode_id", "person_id", "provider", "provider_path")


def upgrade() -> None:
    # The CHECK body follows the column automatically -- Postgres stores a parse
    # tree, not the text -- so only the constraint's *name* has to move. It is
    # moved rather than left, because `ck_images_remote_url_not_empty` on a column
    # called `provider_path` sends the next reader looking for a column that is gone.
    op.alter_column("images", "remote_url", new_column_name="provider_path")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_remote_url_not_empty TO ck_images_provider_path_not_empty"
    )

    # `op.execute`, not `op.create_unique_constraint`: alembic's operation has no
    # parameter for `NULLS NOT DISTINCT`, and without it two of the three owner
    # columns are NULL on every row, so the constraint never fires. A migration
    # that quietly emitted the default would ship the inert spelling.
    op.execute(
        "ALTER TABLE images ADD CONSTRAINT uq_images_owner_provider_path "
        f"UNIQUE NULLS NOT DISTINCT ({', '.join(_KEY_COLUMNS)})"
    )


def downgrade() -> None:
    op.drop_constraint("uq_images_owner_provider_path", "images", type_="unique")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_provider_path_not_empty TO ck_images_remote_url_not_empty"
    )
    op.alter_column("images", "provider_path", new_column_name="remote_url")
