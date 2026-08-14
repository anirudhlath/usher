"""The embedding width moves 384 -> 1024, and the fingerprint scheme cannot help.

Revision ID: m09e
Revises: m09d
Create Date: 2026-08-13

`BAAI/bge-m3` is 1024 wide. `title_embeddings.embedding` and
`user_taste.centroid` were both `halfvec(384)`, so this is DDL.

**Why there is a migration here at all, when the whole point of `model_name`
was that there would not be.** `Embedder.model_name` records the runtime *and*
the checkpoint precisely so a model swap invalidates every stored vector
through `e.model_name IS DISTINCT FROM :model_name` -- the backfill re-claims
them, the stale gauge climbs and drains, and nobody writes a migration. That
mechanism is intact and it is **scoped to a same-width swap**, because the
width is `halfvec`'s typmod and a typmod is DDL. Nothing in the codebase said
so until this revision; `db/models/search.py` now does.

**Every affected row is deleted rather than converted, and the deletes come
before the `ALTER`.** There is no honest conversion -- a 384-lane vector
padded or projected to 1024 is not what the new model would have produced --
and `halfvec(384) -> halfvec(1024)` is a runtime dimension error the moment
one row exists. Emptied first, the type change has nothing to cast.

Deletion is also the *correct* end state rather than merely the possible one,
and the distinction matters for `title_embeddings`: a row there with a NULL
`embedding` is a **written refusal** ("the composed document was degenerate,
do not re-claim this"), so nulling the column instead of deleting the row
would mark all 130,673 titles permanently refused and the backfill would never
touch them again. See `TitleEmbeddingRow`'s class docstring.

## `title_neighbors` is emptied too, and that is a gap being papered over

`title_neighbors` holds no vector, so nothing forces its hand here. It is
emptied because every row in it was computed from vectors this revision
destroys -- and **`blend_fingerprint()` cannot tell.** That function hashes
`_WEIGHTS`, `_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL`: what a score
*means*, in the blend's terms. The embedding model is not one of its inputs,
so a swap of the model leaves every neighbour row reading as current, in
`[0, 1]`, with a plausible `rank`, derived from a model the deployment no
longer runs. The `usher.similarity.neighbors.stale` gauge would read zero
throughout.

That is the same defect ADR-0020 and the `blend_fingerprint` column were
introduced to close one milestone earlier, arriving through a door nobody
checked. **This revision fixes the instance and not the class.** The class
fix is to feed the embedder's `model_name` into `blend_fingerprint()`, which
changes its signature and all three of its consumers; it is recorded in
`.claude/rules/search-and-embeddings.md` as the follow-up rather than smuggled
into a width migration.

## What has to run afterwards

The catalog is left with **zero** embeddings, zero taste centroids and zero
neighbours, and none of it comes back on its own:

    uv run usher index --backfill      # enqueue one index job per stale title
    uv run usher work --once           # (repeat until the queue drains)
    uv run usher similar --rebuild     # nothing does this for you

`downgrade()` is symmetric and equally destructive. It restores the width and
not the data, because the data it would restore was 1024 wide.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "m09e"
down_revision: str | Sequence[str] | None = "m09d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The width this revision moves to and away from. Deliberately literals here
#: rather than an import of `EMBEDDING_DIMENSIONS`: a migration records what it
#: *did*, and a revision whose DDL changes meaning when a constant is next
#: edited is a revision that cannot be replayed. `ffa` imports the constant and
#: is the precedent this departs from on purpose -- it was minted at a time
#: when the number had never moved.
_NEW_WIDTH = 1024
_OLD_WIDTH = 384


def _resize(width: int) -> None:
    """Empty the three derived tables, then move both vector columns to `width`.

    One helper for both directions because the two are genuinely the same
    operation at a different number, and a hand-mirrored `downgrade()` is how
    a pair of these drift.
    """
    # Order is load-bearing twice. The deletes precede the `ALTER` because a
    # dimension change over a populated column is a runtime cast error; and
    # `title_neighbors` precedes `title_embeddings` for the reader rather than
    # for the database -- there is no foreign key between them, and a reader
    # who sees the derived table emptied first is being told which way the
    # derivation runs.
    op.execute(sa.text("DELETE FROM title_neighbors"))
    op.execute(sa.text("DELETE FROM user_taste"))
    op.execute(sa.text("DELETE FROM title_embeddings"))

    # Dropped rather than left in place: an HNSW index cannot survive its
    # column's type changing, and rebuilding an empty one costs nothing.
    op.drop_index("ix_title_embeddings_hnsw", table_name="title_embeddings")

    op.alter_column(
        "title_embeddings",
        "embedding",
        existing_type=HALFVEC(_OLD_WIDTH if width == _NEW_WIDTH else _NEW_WIDTH),
        type_=HALFVEC(width),
        existing_nullable=True,
    )
    op.alter_column(
        "user_taste",
        "centroid",
        existing_type=HALFVEC(_OLD_WIDTH if width == _NEW_WIDTH else _NEW_WIDTH),
        type_=HALFVEC(width),
        existing_nullable=True,
    )

    # `fb4e0a7d2c15`'s parameters, unchanged and spelled out rather than
    # unpacked from a dict -- `op.create_index`'s keyword arguments are
    # individually typed and `**mapping` collapses them to one value type,
    # which mypy refuses. pgvector's own defaults, kept because that is what
    # M6 measured: 50,000 x halfvec(384) at m=16, ef_construction=64 in
    # 4.109 s into 56 MB. **That measurement is now about a narrower vector
    # than the one being indexed** -- 1024 lanes is 2,048 bytes against 768,
    # so the per-row graph cost rises and M6's 1,170.5 bytes/row projection no
    # longer applies. Re-measured after the backfill rather than guessed here.
    op.create_index(
        "ix_title_embeddings_hnsw",
        "title_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def upgrade() -> None:
    _resize(_NEW_WIDTH)


def downgrade() -> None:
    _resize(_OLD_WIDTH)
