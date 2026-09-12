"""`images` gets the natural key `m09a` was asked for and shipped without."""

from alembic import op

revision = "m09c"
down_revision = "m09a"
branch_labels = None
depends_on = None

#: The owner triple plus the two provider columns, in one place, because
#: `upgrade()` names it and `downgrade()` must not name a different one.
_KEY_COLUMNS = ("title_id", "episode_id", "person_id", "provider", "provider_path")


def upgrade() -> None:
    # The CHECK body follows the column automatically -- Postgres stores a
    # parse tree, not the text -- so only the constraint's *name* has to move,
    # and it is moved rather than left because a constraint called
    # `ck_images_remote_url_not_empty` on a column called `provider_path` is
    # the stale "verified" fact `prd-maintenance.md` calls worse than none.
    op.alter_column("images", "remote_url", new_column_name="provider_path")
    op.execute(
        "ALTER TABLE images RENAME CONSTRAINT "
        "ck_images_remote_url_not_empty TO ck_images_provider_path_not_empty"
    )

    # `op.execute`, not `op.create_unique_constraint`: alembic's operation has
    # no parameter for `NULLS NOT DISTINCT`, and the whole finding above is
    # that the spelling without it is inert for two owner kinds in three. A
    # migration that quietly emitted the default would be the exact defect this
    # revision exists to fix, arriving through the tooling.
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
