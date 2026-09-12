"""staging tables are temporary, so drop the leftovers in public"""

from collections.abc import Sequence

from alembic import op

revision: str = "fc6d2b81a794"
down_revision: str | Sequence[str] | None = "fb4e0a7d2c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every `stg_*` name this project has ever created in `public`, from the five
# repositories that stage. Kept in one place because the eleventh staging
# table added is the one whose leftover nobody remembers to clean up -- and
# `tests/unit/test_staging_ddl.py` is what stops an eleventh being created in
# `public` at all.
_LEFTOVER_STAGING_TABLES = (
    "stg_jobs",
    "stg_watch_states",
    "stg_media_items",
    "stg_seasons",
    "stg_episodes",
    "stg_title_embeddings",
    "stg_titles",
    "stg_ratings",
    "stg_tmdb_ids",
    "stg_crosswalk",
)


def upgrade() -> None:
    for table in _LEFTOVER_STAGING_TABLES:
        op.execute(f"DROP TABLE IF EXISTS public.{table}")


def downgrade() -> None:
    """Deliberately empty -- see the module docstring."""
