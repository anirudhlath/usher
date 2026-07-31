"""ingest pipeline: seasons, episodes, jobs, sync_runs, raw_payloads

Revision ID: e5b8f2c40d17
Revises: d4c9b1e37a05
Create Date: 2026-07-31

Started from `--autogenerate` and then hand-finished, because autogenerate
cannot produce three of the things in here:

1. **The two `set_updated_at` triggers** on `seasons` and `episodes`.
   Triggers are not SQLAlchemy `Table` metadata, so autogenerate never sees
   them in either direction. They matter for exactly the reason the first
   three do: both tables are written by `INSERT ... ON CONFLICT DO UPDATE`
   from a staging table, and SQLAlchemy's `onupdate=` is a Core-side feature
   with no effect on raw SQL. Note the naming: `trg_<table>_set_updated_at`,
   matching the three the core schema created -- not `set_updated_at_<table>`.
2. **CHECK constraint bodies on any table it is not creating from scratch.**
   Autogenerate emits a new table's constraints verbatim from metadata (all
   fourteen below came out correct), but it is blind to a *changed*
   condition -- verified in M1 by loosening a bound and getting an empty
   `pass` migration. Every one below was still read by eye against the model.
3. **Reviewer judgement about what a new foreign key costs.** See the two
   `ix_*_episode_id` indexes below.

The two `ALTER TABLE ... ADD CONSTRAINT` statements for the dangling
`episode_id` columns are ordered after `episodes` is created, and the
downgrade drops them before it. `media_items` gets SET NULL, `watch_states`
gets RESTRICT -- see
[ADR-0010](../../../../../docs/prd/decisions/0010-watch-state-title-fk-restrict.md)
for why those differ.

Each of those FKs also gets an index on the *referencing* column, which
autogenerate did not ask for and the plan did not have. Postgres implements
both `SET NULL` and `RESTRICT` by looking up referencing rows by that
column on every referenced-side DELETE, and neither existing index can
serve it: `uq_media_items_source_external` leads with `source_id` and
`uq_watch_states_user_episode` leads with `user_id`. Without them each
episode deletion is a sequential scan of `media_items` (999,827 episode
rows on the one measured deployment) and of `watch_states` -- and
`episodes.title_id` is `ON DELETE CASCADE`, so deleting one series fires
that once per episode of the series. This is the identical argument the
core schema already made for `ix_watch_states_title_id`.

**Reversible, but a down-then-up round trip is not automatically
data-safe.** `downgrade()` drops `episodes` and cannot touch the two
`episode_id` columns that were pointing into it, so coming back up finds
links to rows that no longer exist. Verified directly against real
Postgres: with one episode-level `watch_states` row present, the second
`upgrade()` failed with a bare `ForeignKeyViolationError`.
`_adopt_links_orphaned_by_an_earlier_downgrade` is what makes that
diagnosable -- it applies each column's own `ON DELETE` rule to the
orphans, which means `media_items` is silently cleaned and `watch_states`
refuses with the offending ids named. That asymmetry is not incidental; it
is ADR-0010 arriving at the one moment it actually costs something.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5b8f2c40d17"
down_revision: str | Sequence[str] | None = "d4c9b1e37a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both are written by `INSERT ... ON CONFLICT DO UPDATE` out of a staging
# table, so `onupdate=` never fires for them. `jobs` is deliberately absent:
# its one writer sets `updated_at` explicitly on every statement, the same
# call `source_credentials` made. `sync_runs` and `raw_payloads` have no
# `updated_at` column at all.
_TRIGGERED_TABLES = ("seasons", "episodes")


def _adopt_links_orphaned_by_an_earlier_downgrade() -> None:
    """Deal with `episode_id` values left dangling by a previous downgrade.

    `downgrade()` drops the `episodes` table but cannot touch the two
    `episode_id` columns, so a database that has been down and is coming
    back up can hold links to episodes that no longer exist. Adding the
    foreign keys over them fails with a bare `ForeignKeyViolationError`
    naming one row -- reproduced directly against real Postgres, which is
    why this function exists. On a first upgrade it is a no-op: nothing has
    ever written either column.

    Each column is handled by *its own* delete rule, because that rule is
    already this schema's stated answer to "the episode went away":

    - `media_items.episode_id` is `ON DELETE SET NULL`, so an orphan is
      NULLed. An unmatched MediaItem is worth keeping; it just loses the
      link. Nothing else on the row depends on it.
    - `watch_states.episode_id` is `ON DELETE RESTRICT`, so an orphan
      **refuses the migration**. A WatchState *is* the thing worth keeping
      (ADR-0010), and neither automatic option is acceptable: deleting the
      row destroys watch history, and NULLing the column violates
      `ck_watch_states_exactly_one_target`. Failing here, with the affected
      ids named, is the only answer that does not lose data silently.
    """
    op.execute("""
        UPDATE media_items SET episode_id = NULL
        WHERE episode_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id = media_items.episode_id)
    """)
    orphans = (
        op.get_bind()
        .exec_driver_sql("""
            SELECT id FROM watch_states
            WHERE episode_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id = watch_states.episode_id)
            LIMIT 10
        """)
        .scalars()
        .all()
    )
    if orphans:
        raise RuntimeError(
            "watch_states rows point at episodes that no longer exist, so "
            "fk_watch_states_episode_id_episodes cannot be added. This happens only after a "
            "downgrade past e5b8f2c40d17, which drops the episodes table and leaves these "
            "links dangling. They are watch history and this migration will not delete them "
            "or blank them (ADR-0010); re-create the episodes they refer to, or repoint them, "
            f"then re-run. First offending watch_states.id values: {[str(i) for i in orphans]}"
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "match", "enrich", "watch_history", name="jobkind", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "parked", name="jobstatus", native_enum=False, length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("traceparent", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("key <> ''", name="ck_jobs_key_not_empty"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name="ck_jobs_priority_range"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("kind", "key", name="uq_jobs_kind_key"),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        [sa.literal_column("priority DESC"), "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_jobs_parked",
        "jobs",
        ["kind"],
        unique=False,
        postgresql_where=sa.text("status = 'parked'"),
    )
    op.create_table(
        "raw_payloads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("provider <> ''", name="ck_raw_payloads_provider_not_empty"),
        sa.CheckConstraint("reference <> ''", name="ck_raw_payloads_reference_not_empty"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_payloads")),
        sa.UniqueConstraint(
            "provider", "kind", "reference", name="uq_raw_payloads_provider_kind_reference"
        ),
    )
    op.create_index("ix_raw_payloads_fetched_at", "raw_payloads", ["fetched_at"], unique=False)
    op.create_table(
        "seasons",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title_id", sa.UUID(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("episode_count", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "episode_count IS NULL OR episode_count >= 0",
            name="ck_seasons_episode_count_non_negative",
        ),
        sa.CheckConstraint("season_number >= 0", name="ck_seasons_season_number_non_negative"),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_seasons_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seasons")),
        sa.UniqueConstraint("title_id", "season_number", name="uq_seasons_title_season_number"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "full", "delta", "watch_state", name="syncrunkind", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running", "completed", "failed", name="syncrunstatus", native_enum=False, length=16
            ),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_matched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_unmatched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_retracted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("items_matched >= 0", name="ck_sync_runs_items_matched_non_negative"),
        sa.CheckConstraint(
            "items_retracted >= 0", name="ck_sync_runs_items_retracted_non_negative"
        ),
        sa.CheckConstraint("items_seen >= 0", name="ck_sync_runs_items_seen_non_negative"),
        sa.CheckConstraint(
            "items_unmatched >= 0", name="ck_sync_runs_items_unmatched_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_sync_runs_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "ix_sync_runs_source_kind_started",
        "sync_runs",
        ["source_id", "kind", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title_id", sa.UUID(), nullable=False),
        sa.Column("season_id", sa.UUID(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("absolute_number", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=16), nullable=True),
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
        sa.CheckConstraint(
            "absolute_number IS NULL OR absolute_number >= 0",
            name="ck_episodes_absolute_number_non_negative",
        ),
        sa.CheckConstraint("episode_number >= 0", name="ck_episodes_episode_number_non_negative"),
        sa.CheckConstraint(
            "runtime_minutes IS NULL OR runtime_minutes >= 0",
            name="ck_episodes_runtime_minutes_non_negative",
        ),
        sa.CheckConstraint("season_number >= 0", name="ck_episodes_season_number_non_negative"),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_episodes_season_id_seasons"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_episodes_title_id_titles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
        sa.UniqueConstraint(
            "title_id",
            "season_number",
            "episode_number",
            name="uq_episodes_title_season_episode",
        ),
    )
    # Partial but NOT unique -- deliberately unlike ix_titles_imdb_id. See
    # db/models/episode.py for the argument (two episode trees for one show
    # share episode IMDb ids, and a unique violation there would abort a
    # whole staged COPY batch that ON CONFLICT cannot absorb).
    op.create_index(
        "ix_episodes_imdb_id",
        "episodes",
        ["imdb_id"],
        unique=False,
        postgresql_where=sa.text("imdb_id IS NOT NULL"),
    )
    op.create_index("ix_episodes_season_id", "episodes", ["season_id"], unique=False)

    # The two dangling M1 columns finally get targets, plus the index each
    # one's referential check needs. See this module's docstring.
    _adopt_links_orphaned_by_an_earlier_downgrade()
    op.create_index("ix_media_items_episode_id", "media_items", ["episode_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_media_items_episode_id_episodes"),
        "media_items",
        "episodes",
        ["episode_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_watch_states_episode_id", "watch_states", ["episode_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_watch_states_episode_id_episodes"),
        "watch_states",
        "episodes",
        ["episode_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Hand-written: autogenerate cannot see triggers at all. set_updated_at()
    # itself already exists -- the core schema created it and this migration
    # must not drop it on the way back down, since three other triggers still
    # reference it.
    for table_name in _TRIGGERED_TABLES:
        op.execute(f"""
            CREATE TRIGGER trg_{table_name}_set_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)


def downgrade() -> None:
    """Downgrade schema."""
    for table_name in reversed(_TRIGGERED_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_set_updated_at ON {table_name}")

    op.drop_constraint(
        op.f("fk_watch_states_episode_id_episodes"), "watch_states", type_="foreignkey"
    )
    op.drop_index("ix_watch_states_episode_id", table_name="watch_states")
    op.drop_constraint(
        op.f("fk_media_items_episode_id_episodes"), "media_items", type_="foreignkey"
    )
    op.drop_index("ix_media_items_episode_id", table_name="media_items")
    op.drop_index("ix_episodes_season_id", table_name="episodes")
    op.drop_index(
        "ix_episodes_imdb_id",
        table_name="episodes",
        postgresql_where=sa.text("imdb_id IS NOT NULL"),
    )
    op.drop_table("episodes")
    op.drop_index("ix_sync_runs_source_kind_started", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("seasons")
    op.drop_index("ix_raw_payloads_fetched_at", table_name="raw_payloads")
    op.drop_table("raw_payloads")
    op.drop_index(
        "ix_jobs_parked", table_name="jobs", postgresql_where=sa.text("status = 'parked'")
    )
    op.drop_index(
        "ix_jobs_claim", table_name="jobs", postgresql_where=sa.text("status = 'pending'")
    )
    op.drop_table("jobs")
