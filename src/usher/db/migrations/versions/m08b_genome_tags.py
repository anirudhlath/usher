"""`genome_tags` — what each of `genome_scores.relevance`'s lanes means."""

import sqlalchemy as sa
from alembic import op

from usher.ports.bulk import GENOME_TAG_COUNT

revision = "m08b"
down_revision = "m08a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "genome_tags",
        # MovieLens' own lane index, never minted here. The primary key *is*
        # the natural key, exactly as `genome_scores.title_id` is: a surrogate
        # would add a column nothing reads while permitting two rows for one
        # lane, a state no consumer could interpret.
        sa.Column("tag_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        # The release fingerprint, and the one column this table exists for.
        # Compared against `genome_scores.genome_revision`, never joined to it.
        sa.Column("genome_revision", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tag_id", name="pk_genome_tags"),
        # The same constant `genome_scores.relevance` declares its width with,
        # so the two cannot drift. The lower bound is as load-bearing as the
        # upper: `tag_id - 1` is a list index and `0` would address lane -1.
        sa.CheckConstraint(
            f"tag_id BETWEEN 1 AND {GENOME_TAG_COUNT}",
            name="ck_genome_tags_tag_id_in_vocabulary",
        ),
        sa.CheckConstraint("tag <> ''", name="ck_genome_tags_tag_not_empty"),
        sa.CheckConstraint("genome_revision <> ''", name="ck_genome_tags_revision_not_empty"),
    )
    # No index beyond the primary key: the whole read is the vocabulary in lane order.


def downgrade() -> None:
    op.drop_table("genome_tags")
