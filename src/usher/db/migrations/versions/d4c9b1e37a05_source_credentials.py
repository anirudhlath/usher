"""source credentials"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4c9b1e37a05"
down_revision: str | Sequence[str] | None = "c7a2e51d8b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_credentials",
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
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
        sa.CheckConstraint("ref <> ''", name="ck_source_credentials_ref_not_empty"),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_credentials_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ref", name=op.f("pk_source_credentials")),
    )
    op.create_index(
        "ix_source_credentials_source_id", "source_credentials", ["source_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_source_credentials_source_id", table_name="source_credentials")
    op.drop_table("source_credentials")
