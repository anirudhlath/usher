"""`credits.source`, `people.imdb_id`, and the dedup key an IMDb load needs."""

from alembic import op

revision = "m09d"
down_revision = "m09c"
branch_labels = None
depends_on = None

#: Named once, because `upgrade()` creates it and `downgrade()` must not drop
#: a differently-spelled one.
_NATURAL_KEY = "ix_credits_source_natural_key"


def upgrade() -> None:
    op.execute("ALTER TABLE people ADD COLUMN imdb_id text")
    op.execute(
        "ALTER TABLE people ADD CONSTRAINT ck_people_imdb_id_not_empty "
        "CHECK (imdb_id IS NULL OR imdb_id <> '')"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_people_imdb_id ON people (imdb_id) WHERE imdb_id IS NOT NULL"
    )

    # Three statements, and the order is the point: nullable, backfilled, then
    # NOT NULL. Never `server_default`, which would outlive this migration and
    # supply a plausible wrong value to a writer that forgot.
    op.execute("ALTER TABLE credits ADD COLUMN source varchar(8)")
    op.execute("UPDATE credits SET source = 'tmdb'")
    op.execute("ALTER TABLE credits ALTER COLUMN source SET NOT NULL")

    # `op.execute` rather than `op.create_index`: alembic's operation has no
    # parameter for `NULLS NOT DISTINCT`, and a migration that quietly emitted
    # the default would ship the inert spelling this revision exists to avoid.
    op.execute(
        f"CREATE UNIQUE INDEX {_NATURAL_KEY} ON credits (title_id, source, billing_order) "
        "NULLS NOT DISTINCT WHERE source <> 'tmdb'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_NATURAL_KEY}")
    op.execute("ALTER TABLE credits DROP COLUMN source")
    op.execute("DROP INDEX ix_people_imdb_id")
    op.execute("ALTER TABLE people DROP CONSTRAINT ck_people_imdb_id_not_empty")
    op.execute("ALTER TABLE people DROP COLUMN imdb_id")
