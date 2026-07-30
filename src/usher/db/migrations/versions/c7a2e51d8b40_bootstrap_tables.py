"""bootstrap tables

Revision ID: c7a2e51d8b40
Revises: b3f1c07d4a92
Create Date: 2026-07-30

The three tables the bulk importers need: import_runs (the checkpoint),
tmdb_ids (Phase 1's crawl universe), id_crosswalk (Phase 2's Wikidata
pairs). No BEFORE UPDATE trigger is created for any of them -- see
db/models/bootstrap.py's module docstring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e51d8b40"
down_revision: str | Sequence[str] | None = "b3f1c07d4a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "id_crosswalk",
        sa.Column("imdb_id", sa.String(length=16), nullable=False),
        sa.Column("tmdb_movie_id", sa.Integer(), nullable=True),
        sa.Column("tmdb_series_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_series_id", sa.Integer(), nullable=True),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("imdb_id <> ''", name="ck_id_crosswalk_imdb_id_not_empty"),
        sa.PrimaryKeyConstraint("imdb_id", name=op.f("pk_id_crosswalk")),
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "failed",
                name="importrunstatus",
                native_enum=False,
                length=16,
            ),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("dataset <> ''", name="ck_import_runs_dataset_not_empty"),
        sa.CheckConstraint("position >= 0", name="ck_import_runs_position_non_negative"),
        sa.CheckConstraint("revision <> ''", name="ck_import_runs_revision_not_empty"),
        sa.CheckConstraint("rows_seen >= 0", name="ck_import_runs_rows_seen_non_negative"),
        sa.CheckConstraint("rows_written >= 0", name="ck_import_runs_rows_written_non_negative"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
        sa.UniqueConstraint("dataset", name=op.f("uq_import_runs_dataset")),
    )
    op.create_table(
        "tmdb_ids",
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("movie", "series", name="titlekind", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("popularity", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("adult", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "exported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("popularity >= 0", name="ck_tmdb_ids_popularity_non_negative"),
        sa.PrimaryKeyConstraint("tmdb_id", "kind", name=op.f("pk_tmdb_ids")),
    )
    op.create_index(
        "ix_tmdb_ids_popularity",
        "tmdb_ids",
        [sa.literal_column("popularity DESC")],
        unique=False,
        postgresql_where=sa.text("NOT adult"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_tmdb_ids_popularity", table_name="tmdb_ids", postgresql_where=sa.text("NOT adult")
    )
    op.drop_table("tmdb_ids")
    op.drop_table("import_runs")
    op.drop_table("id_crosswalk")
