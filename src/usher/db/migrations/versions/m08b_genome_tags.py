"""`genome_tags` — what each of `genome_scores.relevance`'s 1,128 lanes means.

Revision ID: m08b
Revises: m08a
Create Date: 2026-08-07

**`m08b`, zero-padded, per the convention `m08a` opened one revision down.**
That docstring holds the full argument and the correction it embeds
(`sorted(["m8a", "m9a", "m10a"])` puts `m10a` *first*); nothing is re-derived
here. `.claude/rules/db-and-sql.md` named this revision id in advance, which is
the only reason it is not a decision.

## One table, three columns, and the third is why the other two are safe

`ffa` shipped `genome_scores` as one dense `halfvec(1128)` per title and
deliberately did **not** ship the vocabulary. Its own docstring recorded the
cost of that and named this migration's shape in advance — *"a 1,128-row table
plus a loader step in a phase that already reads the file, and one
migration"* — together with what made the deferral safe rather than an
omission: `genome_scores.genome_revision` is already there to check a
vocabulary against. This is that table, and this is that check.

**The failure it exists to prevent is prose, which is what makes it worse than
the one `genome_scores.genome_revision` already prevents.** A cosine taken
across two releases is a number that is wrong and plausible; a *label* taken
across two releases is the sentence "because you like atmospheric,
thought-provoking films" attached to a household that likes neither, rendered
on a screen, with nothing anywhere reporting an error. Two releases'
vocabularies are the same type, the same width, and 1,128 English words each.
So `GenomeRepository.vocabulary(revision)` raises rather than answering across
a mismatch, and an operator counts the condition the same way they count the
sibling one: `SELECT genome_revision, count(*) FROM genome_tags GROUP BY 1`.

**Measured against the real member on 2026-08-07** (`ml-latest/
genome-tags.csv` inside `ml-latest.zip`, 8,359 compressed / 18,103
uncompressed bytes, read through the shipped `CachedDatasetFile.member_lines`):
**1,128 rows**, `tagId` exactly `1…1128` and already ascending, every name
non-empty, no name containing a comma, 1,128 distinct names, longest 65
characters, file CRLF-terminated. The 1,128 is the number `ffa` and PRD 04
already carried and it is confirmed rather than assumed here.

## `tag_id` is `integer`, and the argument is which layer refuses a bad value

Not size: at 1,128 rows `smallint` saves 2.2 kB, which is noise beside the
43 MB of TOAST one table over. `.claude/rules/db-and-sql.md`'s standing trap is
a column *narrower* than the field feeding it — such a value is refused by
asyncpg's own binary encoder, client-side, as an **unnamed** `DataError`
(SQLSTATE `22000`; `curated_rows."position"` at `2**31` is the measured
instance), and through a `COPY` it is a bare `builtins.OverflowError` carrying
no SQLSTATE for `is_row_refusal` to inspect at all. Three decisions keep this
column away from that edge, and the first is the one doing the work:

1. **The ceiling on `tag_id` is a batch precondition, not a field bound.**
   `BulkCatalogRepository.replace_genome_tags` refuses a vocabulary that is
   not exactly `1…n` before it writes anything, so the largest `tag_id` that
   can reach a driver is the length of the sequence it was handed. That check
   also catches the failure a per-row `le=` cannot see — a *gap*, which
   renames every later lane.
2. **`integer` puts the remaining boundary out of reach.** Under `smallint` a
   caller reaches the encoder with a 32,768-element list; under `integer` it
   would need `2**31` elements. Everything below that is refused by
   `ck_genome_tags_tag_id_in_vocabulary` — an `IntegrityError` carrying the
   constraint's own name, which is the classifiable path.
3. **The write is a plain `INSERT`, never `usher.db.staging`.** 1,128 rows do
   not need a `COPY`, and the `COPY` path is where a refusal loses its
   SQLSTATE entirely.

**Measured, not argued.** Both column types, both paths, on a scratch
`pgvector/pgvector:pg17` on 2026-08-07, against this exact CHECK:

    tag_id      integer + CHECK                    smallint + CHECK
    ----------  ---------------------------------  ---------------------------------
    1,128       stored                             stored
    1,129       IntegrityError 23514, named        IntegrityError 23514, named
    32,768      IntegrityError 23514, named        DBAPIError 22000, constraint None
    2**31       DBAPIError 22000, constraint None   DBAPIError 22000, constraint None

("named" is `constraint_name(exc) == "ck_genome_tags_tag_id_in_vocabulary"`;
the unnamed rows are `exc.orig.__cause__` = `asyncpg.exceptions.DataError`,
refused client-side by asyncpg's own encoder before a byte is sent.) The one
cell that decides the type is `smallint` at 32,768. And through
`stage_records`, on the same run: 1,129 and 32,768 **stage successfully** —
staging tables carry no constraints — and `2**31` raises
`builtins.OverflowError: value out of int32 range` with **no SQLSTATE at
all**, which `is_row_refusal` cannot inspect. That is decision 3.

## The two CHECKs beyond the range, and what each refuses

- `ck_genome_tags_tag_not_empty` — a lane named by the empty string reads as
  labelled and says nothing, which is worse than a missing row, because a
  missing row is what the contiguity precondition catches. **It is `tag <> ''`
  and not `btrim(tag) <> ''`, which means it accepts `'   '`**; the loader
  refuses that (`MovieLensGenomeDataset._vocabulary` checks `not name.strip()`)
  and all 1,128 measured names are `strip()`-stable, so tightening the CHECK
  would be a migration for a value nothing can currently produce. Recorded
  here rather than left for the next reader to re-derive from the SQL.
- `ck_genome_tags_revision_not_empty` — an empty revision matches no
  `genome_scores` row, so every read of the vocabulary would refuse and the
  table would be silently inert. `genome_scores.genome_revision` carries no
  such CHECK; it is added here rather than back-filled there because a
  migration that touches `ffa`'s table is not this task's, and the asymmetry
  is recorded rather than left to be discovered.

## No index beyond the primary key, and no `computed_at`

`genome_scores`' and `llm_calls`' precedent for the first: the only read this
table has is the whole of it in `tag_id` order, which the primary key already
serves, and an index nothing reads is `ix_titles_popularity` again.

For the second, the asymmetry with `genome_scores.computed_at` is real. A
genome vector's age is not recoverable from `import_runs`, because a resumed
import writes different movies in different runs; this table is written by one
`DELETE` + one `INSERT` in one transaction, so its age *is* the
`movielens.genome` row an operator already reads with `usher bootstrap-status`.
A fourth column would be a second, drift-capable copy of that timestamp.

**No `updated_at` and no trigger**, which is mechanically required as well as
right: `tests/integration/test_migrations.py::
test_migration_creates_the_updated_at_triggers` asserts the trigger set
**exactly**, so a trigger here is a failing case in another file.

**No foreign key to `genome_scores`**, and there is nothing to point one at: a
vocabulary row is keyed on a lane index and a vector row on a title id. The
relationship between the two tables is the equality of a `text` column, which
is what a repository compares and what no referential constraint can express.

Reversible in both directions. `downgrade()` drops exactly the one table this
creates, and its constraints go with it. Verified empty → head →
`downgrade base` → head against a real `pgvector/pgvector:pg17`.
"""

import sqlalchemy as sa
from alembic import op

from usher.ports.bulk import GENOME_TAG_COUNT

revision = "m08b"
down_revision = "m08a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "genome_tags",
        # MovieLens' own lane index, never minted here. The primary key *is*
        # the natural key, exactly as `genome_scores.title_id` is: a surrogate
        # would add a column nothing reads while permitting two rows for one
        # lane, a state no consumer could interpret.
        sa.Column("tag_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        # ADR-0020's fingerprint, and the one column this table exists for.
        # Compared against `genome_scores.genome_revision`, never joined to it.
        sa.Column("genome_revision", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("tag_id", name="pk_genome_tags"),
        # The same constant `genome_scores.relevance` declares its width with,
        # so the two cannot drift. The lower bound is as load-bearing as the
        # upper: `tag_id - 1` is a list index and `0` would address lane -1.
        sa.CheckConstraint(
            f"tag_id BETWEEN 1 AND {GENOME_TAG_COUNT}",
            name="ck_genome_tags_tag_id_in_vocabulary",
        ),
        sa.CheckConstraint("tag <> ''", name="ck_genome_tags_tag_not_empty"),
        sa.CheckConstraint("genome_revision <> ''", name="ck_genome_tags_revision_not_empty"),
    )
    # No index beyond the primary key -- see this migration's docstring.


def downgrade() -> None:
    op.drop_table("genome_tags")
