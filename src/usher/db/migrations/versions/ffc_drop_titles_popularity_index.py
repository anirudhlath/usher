"""`ix_titles_popularity` goes, because no statement can use it as declared."""

from alembic import op

revision = "ffc"
down_revision = "ffb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_titles_popularity", table_name="titles")


def downgrade() -> None:
    op.create_index(
        "ix_titles_popularity",
        "titles",
        ["popularity"],
        unique=False,
        postgresql_where="popularity IS NOT NULL",
        postgresql_using="btree",
        postgresql_ops={"popularity": "DESC"},
    )
