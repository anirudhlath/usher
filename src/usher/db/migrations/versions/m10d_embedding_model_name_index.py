"""`title_embeddings.model_name` gets the index the model guard reads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10d"
down_revision: str | Sequence[str] | None = "m10c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `SELECT DISTINCT model_name ... WHERE embedding IS NOT NULL` had nothing
    # to read but the heap, and since `m09f` that heap carries its 1024-lane
    # halfvecs inline -- so naming one string cost a scan of every vector in
    # the catalog. Partial on the read's own predicate, so the index holds
    # exactly the population the guard asks about and a row with no vector --
    # never a seed -- costs nothing to keep in it.
    op.create_index(
        "ix_title_embeddings_model_name",
        "title_embeddings",
        ["model_name"],
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_title_embeddings_model_name", table_name="title_embeddings")
