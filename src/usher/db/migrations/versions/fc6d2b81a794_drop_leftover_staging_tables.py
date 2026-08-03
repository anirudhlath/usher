"""staging tables are temporary, so drop the leftovers in public

Revision ID: fc6d2b81a794
Revises: fb4e0a7d2c15
Create Date: 2026-08-03

`usher.db.staging` now creates `CREATE TEMP TABLE ... ON COMMIT DROP`, so
nothing in `src/` will ever put a `stg_*` relation in `public` again. This
drops the ones a release predating that change may have left behind.

**Why a migration and not a one-line note in an upgrade guide.** Postgres DDL
is transactional, so a staging table survives exactly when its caller
*commits* -- which is every walk, every enqueue on the pipeline's hot path,
and every crashed batch in between. Three things a leftover then does, in
increasing order of how hard it is to see:

1. It is schema drift. `test_migration_matches_the_orm_metadata` compares
   `inspect(conn).get_table_names()` against `Base.metadata` and a
   `public.stg_jobs` is a table the models do not have. (A *temporary* table
   is invisible to that call, which is what deletes the drift rather than
   working around it -- nine integration files carried an explicit
   `DROP TABLE IF EXISTS stg_*` cleanup for this and no longer do.)
2. It is stale rows with the destination's shape and none of its
   constraints, sitting under a name every session in the deployment shares.
3. It is a one-time `ACCESS EXCLUSIVE` stall. `stage_records`' drop is
   `pg_temp`-qualified precisely so a leftover cannot be reached -- measured
   at 819 ms of lockstep waiting when it was not -- so this migration is
   *cleanup*, not a prerequisite for correctness. Both halves matter: the
   qualified drop means a deployment that skips this migration is slow at
   nothing and correct at everything, and this migration means it is not
   carrying dead tables either.

All ten names, enumerated rather than globbed. `DROP TABLE IF EXISTS
public.stg_*` is not valid SQL and a `DO $$` block over `pg_class LIKE
'stg\\_%'` would drop whatever an operator happened to name that way -- these
ten are this project's, and a wildcard over someone else's schema is a
migration that destroys data it was never told about.

`downgrade()` is a documented no-op. Recreating an empty staging table would
restore the hazard rather than the state: nothing reads one that
`stage_records` did not create moments earlier in the same transaction, so
there is no data and no dependency to put back.
"""

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
