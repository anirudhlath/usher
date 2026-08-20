"""Three columns had two writers each; now five columns each name their source.

Revision ID: m10a
Revises: m09f
Create Date: 2026-08-19

`titles.vote_count` was written by `adapters/bulk/imdb.py` with IMDb's
`numVotes` and by `adapters/tmdb/mapping.py` with TMDb's `vote_count`, which
are the same concept counted over different electorates. **The gap is ~36x on
the only paired sample this project has taken**: 537 tier movies enriched
2026-08-11, median TMDb `vote_count` **16** against median IMDb `numVotes`
**581**, re-measured at S3's scale over 130,647 titles as 15 against 576
(`scripts/enqueue_tier_enrichment.py`). Paired, i.e. the same rows counted
both ways -- a ratio of the two maxima in the table below would be ~65x and
would mean nothing, because those maxima come from disjoint populations.
`community_rating` was dual-written the same way and **silently**, because
IMDb's `averageRating` and TMDb's `vote_average` are both 0-10 and no value is
ever out of range.

Measured on the deployed 1,272,870-title catalog at `m09f`, 2026-08-19:

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

**The two ranges overlap among movies**, which is the fact that decides the
repair: 40,518 against 40,695 means no threshold, ratio or magnitude rule can
tell a contaminated row from a clean one.

**`enrichment_state` cannot tell them apart either, and the reason is an
ordering rather than a count.** Neither writer is gated on it: `apply_ratings`
matches on `imdb_id` alone and TMDb enrichment writes whatever the crawl
reaches, so the two can run in either order and the *last* one to touch a row
owns both rating columns. An IMDb ratings re-import after a crawl leaves a row
marked `enriched` holding IMDb's numbers; a crawl after a re-import leaves a
skeleton holding TMDb's, since enrichment sets the tier in the same write. So
`enriched` is a statement about the last successful fetch and not about which
source a rating came from.

*(An earlier draft of this docstring argued the same conclusion from a
different number -- 237,252 movies carrying a TMDb `popularity` against
131,241 marked `enriched` -- and that number does not support it. Popularity
has a **second** writer the rating columns do not: `link_crosswalk` copies it
from `tmdb_ids` during `--phase crosswalk|all`, on skeleton rows, touching
neither rating column. The gap it measures is that writer, not contamination.
Recorded rather than deleted, because supporting a claim about one column with
evidence about another is one hop from this project's signature failure.)*

`popularity` is renamed although only TMDb ever writes it -- by two different
statements, `tmdb/mapping.py`'s enrichment and `link_crosswalk`'s copy, which
is why it is the one column whose presence says nothing about enrichment.
Leaving one unprefixed TMDb column beside four prefixed ones is the ambiguity
this revision exists to remove.

## This migration moves no rating value, and renames three JSONB keys

The column renames carry the existing bytes and the two new columns arrive
NULL. **No rating value is reinterpreted**, because a rule like *"a
non-enriched row's `vote_count` must be IMDb's"* is an inference, and
inference-quoted-as-measurement is this project's signature failure (issue
#30's deletion claim measured out at 69,160 real deletions). IMDb's numbers
come back from `title.ratings.tsv.gz` -- 8.2 MiB, the authoritative source,
covering the enriched rows whose IMDb values were overwritten as well as the
skeletons. **A `--phase ratings` that re-imports them is owed by Task 3 of
`docs/plans/2026-08-19-rating-provenance-split.md` and does not exist at this
revision** -- `usher bootstrap --phase ratings` exits 2 with `invalid choice`
until it lands, so do not reach for it after rolling this forward. ADR-0040.

**`field_provenance`'s three keys are renamed here, and that is a rename
rather than the data movement this section refuses.** The column is
`field -> provider`, and `adapters/tmdb/mapping.py` derives those keys from
the very `Title` field names this revision moves -- so without this an
already-enriched row keeps `"community_rating": "tmdb"` while its next
enrichment adds `"tmdb_vote_average": "tmdb"` beside it, permanently, because
`services/enrich.py` **merges** provenance rather than assigning it.

The distinction from the rating values is measured and not asserted. On the
deployed catalog, 2026-08-19: **all 132,415 rows carrying any provenance carry
all three of these keys, and every one of the three values is `tmdb` -- there
is not a single `imdb` entry among them.** So the new key's value is *read off
the old key* and never guessed, no row's meaning changes, and there is no
population the rule could be wrong about. That is exactly what the rating
values are not: their correct value is unknowable from the row, which is why
they are re-imported from source instead.

`->` rather than the `?` existence operator throughout, and per key rather
than wholesale: a row carrying two of the three must keep the third absent
rather than gain a null, and `field_provenance -> 'k' IS NOT NULL` is
`field_provenance ? 'k'` exactly (a present key with a JSON `null` value
yields `'null'::jsonb`, which is not SQL NULL) without depending on how a
driver reads a bare `?`.

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
