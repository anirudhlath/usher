"""`seed_rows` -- one stored "Because you watched" row per seed title."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10i"
down_revision: str | Sequence[str] | None = "m10h"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seed_rows",
        sa.Column("seed_title_id", sa.UUID(), nullable=False),
        # Two parallel arrays, ordered: the card order is the product. Each source entry
        # is a card's source names, sorted and joined by `+`.
        sa.Column(
            "card_title_ids", sa.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "card_sources", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        # The last good build: all present or all absent, which the CHECK below holds.
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("built_recipe", sa.Text(), nullable=True),
        # The latest try, whether or not it built anything.
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_outcome",
            sa.Enum(
                "built",
                "too_few",
                "unusable",
                name="seedrowoutcome",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("attempt_recipe", sa.Text(), nullable=False),
        # `SeedRow`'s validators, mirrored: nothing stops a hand-written `INSERT` from
        # bypassing the model. `--autogenerate` cannot see a CHECK body, so each is
        # written out here by hand.
        sa.CheckConstraint(
            "cardinality(card_sources) = cardinality(card_title_ids)",
            name="ck_seed_rows_sources_match_cards",
        ),
        sa.CheckConstraint(
            "(cardinality(card_title_ids) > 0) = (built_at IS NOT NULL) "
            "AND (built_at IS NOT NULL) = (built_recipe IS NOT NULL)",
            name="ck_seed_rows_built_iff_cards",
        ),
        sa.CheckConstraint(
            "cardinality(card_title_ids) = 0 OR "
            "(cardinality(card_title_ids) >= 8 AND cardinality(card_title_ids) <= 20)",
            name="ck_seed_rows_card_count",
        ),
        sa.CheckConstraint(
            "(built_recipe IS NULL OR length(built_recipe) = 32) AND length(attempt_recipe) = 32",
            name="ck_seed_rows_recipe_length",
        ),
        # CASCADE, `title_neighbors`' case: a record is built again by the scheduler, so it
        # protects nothing a delete would destroy. The key leads the cascade's own lookup.
        sa.ForeignKeyConstraint(
            ["seed_title_id"],
            ["titles.id"],
            name=op.f("fk_seed_rows_seed_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("seed_title_id", name=op.f("pk_seed_rows")),
    )


def downgrade() -> None:
    op.drop_table("seed_rows")
