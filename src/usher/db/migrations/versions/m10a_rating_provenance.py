"""Three columns had two writers each; now five columns each name their source."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10a"
down_revision: str | Sequence[str] | None = "m09f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(old, new)` for every column this revision renames, and the constraint that
#: travels with each. One tuple rather than three pairs of statements, so a
#: fourth is one line and `downgrade()` cannot fall out of step with
#: `upgrade()`.
_RENAMES: tuple[tuple[str, str, str, str], ...] = (
    (
        "community_rating",
        "tmdb_vote_average",
        "ck_titles_community_rating_range",
        "ck_titles_tmdb_vote_average_range",
    ),
    (
        "vote_count",
        "tmdb_vote_count",
        "ck_titles_vote_count_non_negative",
        "ck_titles_tmdb_vote_count_non_negative",
    ),
    (
        "popularity",
        "tmdb_popularity",
        "ck_titles_popularity_non_negative",
        "ck_titles_tmdb_popularity_non_negative",
    ),
)


def _rename_provenance_keys(pairs: Sequence[tuple[str, str]]) -> str:
    """`UPDATE titles` moving `field_provenance`'s keys from `old` to `new`.

    One builder for both directions, so `downgrade()` cannot rename a subset
    `upgrade()` renamed -- the same argument `_RENAMES` makes for the columns,
    and it matters more here because a half-renamed JSONB key is invisible to
    every schema reader in `test_migrations.py`.

    Per key rather than wholesale: a row carrying two of the three keeps the
    third **absent** rather than gaining a null. The values are interpolated
    from `_RENAMES`, which is a module constant, so there is no injection
    surface -- S608's own carve-out, and the reason `link_crosswalk` carries
    the same `noqa`.
    """
    strip = "".join(f" - '{old}'" for old, _ in pairs)
    adds = "\n           || ".join(
        f"CASE WHEN field_provenance -> '{old}' IS NOT NULL "
        f"THEN jsonb_build_object('{new}', field_provenance -> '{old}') "
        f"ELSE '{{}}'::jsonb END"
        for old, new in pairs
    )
    where = " OR ".join(f"field_provenance -> '{old}' IS NOT NULL" for old, _ in pairs)
    return f"""
        UPDATE titles
        SET field_provenance = (field_provenance{strip})
           || {adds}
        WHERE {where}
    """  # noqa: S608 -- every fragment is built from `_RENAMES`, a module constant


def upgrade() -> None:
    for old, new, old_check, new_check in _RENAMES:
        op.alter_column("titles", old, new_column_name=new)
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {old_check} TO {new_check}")

    op.execute(_rename_provenance_keys([(old, new) for old, new, _, _ in _RENAMES]))

    op.add_column("titles", sa.Column("imdb_average_rating", sa.Float(), nullable=True))
    op.add_column("titles", sa.Column("imdb_num_votes", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_titles_imdb_average_rating_range",
        "titles",
        "imdb_average_rating IS NULL OR imdb_average_rating BETWEEN 0 AND 10",
    )
    op.create_check_constraint(
        "ck_titles_imdb_num_votes_non_negative",
        "titles",
        "imdb_num_votes IS NULL OR imdb_num_votes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_titles_imdb_num_votes_non_negative", "titles", type_="check")
    op.drop_constraint("ck_titles_imdb_average_rating_range", "titles", type_="check")
    op.drop_column("titles", "imdb_num_votes")
    op.drop_column("titles", "imdb_average_rating")

    op.execute(_rename_provenance_keys([(new, old) for old, new, _, _ in _RENAMES]))

    for old, new, old_check, new_check in _RENAMES:
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {new_check} TO {old_check}")
        op.alter_column("titles", new, new_column_name=old)
