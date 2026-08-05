"""The MovieLens tag genome as one dense halfvec per title.

Revision ID: ffa
Revises: ff
Create Date: 2026-08-04

**One table, `genome_scores`, and boundary call 7 is the whole of it.** PRD
02 implies a tall `(title_id, tag_id, relevance)` shape; this is where that
is refused, with the measurement rather than the argument. Priced on a
scratch `pgvector/pgvector:pg17` (pgvector 0.8.6) at the real dimensions:

    form                                          rows   total size
    ------------------------------------  ------------  -----------
    halfvec(1128), one row per title             16,376        45 MB
    real[], one row per title                    16,376        88 MB
    (title_id, tag_id smallint, relevance)   18,472,128     2,106 MB

**47x**, against a database PRD 08 budgets at 8-12 GB *total*. The 45 MB
splits 1,096 kB heap + 43 MB TOAST + 624 kB index, and that index is this
table's primary key -- **no other index ships**, deliberately. A full
pairwise cosine over all 16,376 vectors is 1.190 ms on a `Seq Scan`; the
access pattern is a pair lookup by `title_id` rather than a KNN; and M6
measured a planner-*preferred* index costing 4.3x for byte-identical recall.
`tests/integration/test_genome_repository.py` asserts the index set so a
later migration cannot quietly add an HNSW one "for similarity".

**This migration creates no extension.** `fa2b6c1e9d30` already creates
`vector` (plus `pg_trgm` and `fuzzystrmatch`) `IF NOT EXISTS` and never drops
them. That migration's docstring records the asymmetry, and it now applies to
a **second** dependent column: `DROP EXTENSION vector` fails while any object
depends on it, and after this migration that includes
`genome_scores.relevance` as well as `title_embeddings.embedding`. `DROP
EXTENSION vector CASCADE` is the version that "works", and it works by
silently dropping the dependent columns -- which is what ADR-0010 refuses one
layer down. Nothing about that changes here; the note simply gains a second
dependent, and this migration's own downgrade drops only its own table.

**The revision id extends by a character, and `ff` was the last two-character
one.** `fa2b6c1e9d30`'s convention fixes one hex character per migration;
M6's cycle ended at `fc`, M7 spent `fd`/`fe`/`ff`, and no hex character sorts
after `f`. So `ff` -> `ffa` -> `ffb`, which is unbounded and still sorts
correctly under a plain `ls` -- rather than a third cycle starting with a
digit, which would sort *before* `fa` and lose the only thing the convention
ever bought. Alembic orders by `down_revision` and never cared; `ls` order
matching chain order is what is being preserved.

**Group G amends this revision rather than adding a fifth.** `user_taste`
belongs in `db/models/taste.py` beside `genome_scores` and in this same
migration. The branch is unreleased and its head has not shipped, so amending
an unmerged revision is legal; if a developer database has already applied
it, `alembic downgrade -1` then `alembic upgrade head` is the recovery. Said
here so group G finds the instruction where it will be looking.

Reversible in both directions. Verified empty -> head -> `downgrade base` ->
head against a real `pgvector/pgvector:pg17`.
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

from usher.ports.bulk import GENOME_TAG_COUNT

revision = "ffa"
down_revision = "ff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "genome_scores",
        # The primary key *is* the foreign key, exactly as
        # `title_embeddings`: one vector per title, and a surrogate id would
        # add a column nothing reads while permitting two rows per title.
        #
        # CASCADE, and it is the `title_embeddings` case rather than the
        # `watch_states` one. ADR-0010 makes `watch_states.title_id` RESTRICT
        # because a watch state is user state a delete would destroy
        # silently; a genome vector is neither user state nor irrecoverable.
        # After a repointing merge the loser's vector describes a film that
        # is no longer the canonical title, so it dies with the loser rather
        # than blocking the delete or surviving attached to nothing.
        sa.Column(
            "title_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("titles.id", ondelete="CASCADE", name="fk_genome_scores_title_id_titles"),
            primary_key=True,
        ),
        # NOT NULL, unlike `title_embeddings.embedding`. That column is
        # nullable so a *refusal* has somewhere to be written; the genome has
        # no analogous outcome (a run of the wrong length is
        # `PortDataMalformed`, not a row), so the only two states are "has a
        # row" and "does not" and the absence of the row is the signal.
        sa.Column("relevance", HALFVEC(GENOME_TAG_COUNT), nullable=False),
        # ADR-0020: derived state carries its fingerprint. The tag vocabulary
        # can change between releases and two vectors from different releases
        # are type-identical and same-width, so a mixed table yields cosines
        # that are wrong and plausible. `GenomeRepository.get_pair` refuses
        # across a mismatch, and an operator counts one with
        # `SELECT genome_revision, count(*) FROM genome_scores GROUP BY 1`.
        sa.Column("genome_revision", sa.Text(), nullable=False),
        # `computed_at` and no `updated_at`, and no trigger: this follows
        # `title_neighbors`, where a row is a batch artefact computed
        # wholesale by one pass. `test_migration_creates_the_updated_at_
        # triggers` asserts the trigger set exactly, so this table adds none.
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("title_id", name="pk_genome_scores"),
    )


def downgrade() -> None:
    op.drop_table("genome_scores")
