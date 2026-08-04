"""title_embeddings and title_neighbors, with the HNSW index

Revision ID: fb4e0a7d2c15
Revises: fa2b6c1e9d30
Create Date: 2026-08-02

Second in M6's `fa.../fb.../fc...` cycle -- see fa2b6c1e9d30's docstring for
why the one-hex-letter convention restarted there.

`vector` is installed by fa2b6c1e9d30, not here. All three extensions live
in one migration so there is one place to read to know what this database
needs, and one place for the argument about why none of them is dropped on
the way down.

**Started from `--autogenerate` and hand-finished.** What it got right is
worth recording, because the plan predicted otherwise: it *did* emit
`HALFVEC(dim=384)` and it *did* carry every one of the HNSW index's
options -- `postgresql_using`, `postgresql_ops`, `postgresql_with` and
`postgresql_where` -- through to the generated file. The one thing it could
not do is **import** the type: it rendered
`pgvector.sqlalchemy.halfvec.HALFVEC(dim=384)` with no matching import, so
the generated module raises `NameError` on import. That failure is loud
rather than silent, which is the better direction, but it still means this
file cannot be used as generated.

The second hand-finish is judgement autogenerate does not have:
`title_neighbors.neighbor_id` is `ON DELETE CASCADE` and needs an index on
the *referencing* column, which nothing asks for -- the same reviewer
judgement M4's migration recorded for the two `episode_id` columns.

**`CREATE INDEX CONCURRENTLY` cannot run here at all.** `env.py` wraps both
migration modes in `context.begin_transaction()`, so the choice a populated
table would force -- lock for the length of the build, or build outside the
migration through an operator command -- does not arise: **this migration
creates the table in the same breath, so the HNSW index is built over zero
rows.** It locks nothing that exists and costs nothing measurable.

The consequence is not free and is recorded rather than glossed: the graph
is then grown *incrementally* by the backfill's inserts rather than by a
bulk build, which is slower per row and produces a slightly different graph
than the 4.109 s bulk figure describes. At the 2k-10k rows boundary call 4
actually embeds, the difference is seconds. For a future whole-catalog
embedding the right shape is the opposite one -- populate, raise
`maintenance_work_mem`, then `REINDEX` -- and the numbers that say so are in
db/models/search.py.

**Neither table gets a `set_updated_at` trigger**, so
`tests/integration/test_migrations.py::test_migration_creates_the_updated_at_triggers`
and its literal name set are unchanged by this migration. Assert that
rather than assume it: that case exists precisely because a new table with a
trigger has to join the set in the same commit.

Reversible, and cheap in both directions: two tables and their indexes, no
data movement, nothing outside them touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "fb4e0a7d2c15"
down_revision: str | Sequence[str] | None = "fa2b6c1e9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "title_embeddings",
        sa.Column("title_id", sa.UUID(), nullable=False),
        # Nullable, and it is load-bearing: it is how a refused degenerate
        # document stops matching the stale predicate. See
        # db/models/search.py's class docstring.
        sa.Column("embedding", HALFVEC(384), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("model_name <> ''", name="ck_title_embeddings_model_name_not_empty"),
        sa.CheckConstraint(
            "source_fingerprint <> ''", name="ck_title_embeddings_fingerprint_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_title_embeddings_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("title_id", name=op.f("pk_title_embeddings")),
    )
    op.create_index(
        "ix_title_embeddings_hnsw",
        "title_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )
    op.create_table(
        "title_neighbors",
        sa.Column("title_id", sa.UUID(), nullable=False),
        sa.Column("neighbor_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("title_id <> neighbor_id", name="ck_title_neighbors_not_self"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_title_neighbors_score_range"),
        sa.CheckConstraint("rank >= 0", name="ck_title_neighbors_rank_non_negative"),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_title_neighbors_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["neighbor_id"],
            ["titles.id"],
            name=op.f("fk_title_neighbors_neighbor_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("title_id", "neighbor_id", name="pk_title_neighbors"),
    )
    op.create_index("ix_title_neighbors_neighbor_id", "title_neighbors", ["neighbor_id"])


def downgrade() -> None:
    op.drop_index("ix_title_neighbors_neighbor_id", table_name="title_neighbors")
    op.drop_table("title_neighbors")
    op.drop_index("ix_title_embeddings_hnsw", table_name="title_embeddings")
    op.drop_table("title_embeddings")
