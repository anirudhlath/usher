"""Three columns had two writers each; now five columns each name their source.

Revision ID: m10a
Revises: m09f
Create Date: 2026-08-19

`titles.vote_count` was written by `adapters/bulk/imdb.py` with IMDb's
`numVotes` and by `adapters/tmdb/mapping.py` with TMDb's `vote_count`, which
are the same concept on scales ~50-100x apart. `community_rating` was
dual-written the same way and **silently**, because IMDb's `averageRating` and
TMDb's `vote_average` are both 0-10 and no value is ever out of range.

Measured on the deployed 1,272,870-title catalog at `m09f`, 2026-08-19:

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

**The two ranges overlap among movies**, which is the fact that decides the
repair: 40,518 against 40,695 means no threshold, ratio or magnitude rule can
tell a contaminated row from a clean one. `enrichment_state` cannot either --
237,252 movies carry a TMDb `popularity` and only 131,241 are marked
`enriched`, so TMDb data reached rows that column does not name.

`popularity` is renamed although it has only one writer. Leaving one
unprefixed TMDb column beside four prefixed ones is the ambiguity this
revision exists to remove.

## This migration moves no data

The renames carry the existing bytes and the two new columns arrive NULL. A
rule like *"a non-enriched row's `vote_count` must be IMDb's"* is an inference,
and inference-quoted-as-measurement is this project's signature failure (issue
#30's deletion claim measured out at 69,160 real deletions). IMDb's numbers come
back from `title.ratings.tsv.gz` -- 8.2 MiB, the authoritative source, covering
the enriched rows whose IMDb values were overwritten as well as the skeletons.
`usher bootstrap --phase ratings` is what runs it. ADR-0040.

## Cost

Every operation here is catalog-only except the two CHECK validations, which
scan. `ALTER TABLE ... RENAME COLUMN` rewrites a `pg_attribute` row;
`ADD COLUMN ... NULL` with no default has not rewritten a table since
PostgreSQL 11. The CHECKs scan 1.27M rows under `ACCESS EXCLUSIVE` and are
trivially satisfied because both columns are NULL on every row at this
revision -- add them here, while that is true, rather than after the backfill
when it is not.

**A CHECK body follows its column automatically** -- Postgres stores a parse
tree, not the text -- so only each constraint's *name* has to move. `m09c` is
the precedent and carries the same two-line idiom.
"""

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


def upgrade() -> None:
    for old, new, old_check, new_check in _RENAMES:
        op.alter_column("titles", old, new_column_name=new)
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {old_check} TO {new_check}")

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

    for old, new, old_check, new_check in _RENAMES:
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {new_check} TO {old_check}")
        op.alter_column("titles", new, new_column_name=old)
