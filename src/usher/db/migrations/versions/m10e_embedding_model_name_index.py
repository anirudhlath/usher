"""`title_embeddings.model_name` gets the index the model guard reads.

Revision ID: m10e
Revises: m10d
Create Date: 2026-09-12

`SimilarityService.foreign_embedding_models` is a `DISTINCT` over `model_name`
scoped to the rows that have a vector, and with no index it walks a heap that
stores its `halfvec`s inline (`m09f`) to name one string. `downgrade()` is
load-bearing: `title_embeddings` outlives this revision and nothing else drops
the index.

## Cost

**`CREATE INDEX CONCURRENTLY` cannot run here**, because `env.py` wraps both
migration modes in `context.begin_transaction()` -- `ff_row_read_indexes.py`'s
finding, and `m10d` one revision down hit it too. `title_embeddings` is
populated on any deployment that has run the embedding backfill, so
`CREATE INDEX` takes a SHARE lock that blocks writes to it for the length of
the build. The writers are that backfill and the enrichment lane. An operator
who cannot take that window builds the index by hand with `CREATE INDEX
CONCURRENTLY` and then `alembic stamp`s this revision; that is an operator
escape rather than the default, and it is recorded here rather than discovered
at 3am.

**Partial, so the build reads less than the table.** The predicate is the
read's own -- a row with a refused embedding is never a seed and names no
vector a pool can be drawn from -- which keeps written refusals out of the
index and out of its maintenance.
"""

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
