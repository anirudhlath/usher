"""`title_embeddings.model_name` gets the index the model guard reads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10e"
down_revision: str | Sequence[str] | None = "m10d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_title_embeddings_model_name",
        "title_embeddings",
        ["model_name"],
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_title_embeddings_model_name", table_name="title_embeddings")
