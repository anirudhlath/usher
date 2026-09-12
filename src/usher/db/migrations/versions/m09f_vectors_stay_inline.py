"""Every `halfvec` column stores PLAIN, because 1024 lanes crossed the TOAST line."""

from collections.abc import Sequence

from alembic import op

revision: str = "m09f"
down_revision: str | Sequence[str] | None = "m09e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every `halfvec` column in the schema, checked against `pg_type` rather than
#: remembered: `title_embeddings.embedding` and `user_taste.centroid` from
#: `m09e`, `genome_scores.relevance` from `ffa`. If a fourth is ever added it
#: belongs here, and `test_every_halfvec_column_stores_inline` is what fails if
#: it is not.
_VECTOR_COLUMNS = (
    ("title_embeddings", "embedding"),
    ("user_taste", "centroid"),
    ("genome_scores", "relevance"),
)

#: pgvector's declared default for `halfvec`, which is what `downgrade()`
#: restores. Read off `pg_attribute.attstorage` rather than assumed.
_TYPE_DEFAULT = "EXTERNAL"


def _set_storage(mode: str) -> None:
    """Set the storage mode on every vector column and rewrite each table.

    The `VACUUM FULL` is what moves values that already exist; without it this
    migration changes only how future rows are written, which is the failure
    mode that would make it look applied and measure unchanged.
    """
    for table, column in _VECTOR_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STORAGE {mode}")
    # Outside the migration's transaction, because `VACUUM` refuses to run in
    # one. Each table separately rather than a bare `VACUUM FULL`: this touches
    # three relations and a database-wide rewrite would take every other table
    # with it, including the 1.27M-row `titles`.
    with op.get_context().autocommit_block():
        for table, _ in _VECTOR_COLUMNS:
            op.execute(f"VACUUM FULL {table}")


def upgrade() -> None:
    _set_storage("PLAIN")


def downgrade() -> None:
    _set_storage(_TYPE_DEFAULT)
