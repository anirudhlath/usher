"""Every `halfvec` column stores PLAIN, because 1024 lanes crossed the TOAST line.

Revision ID: m09f
Revises: m09e
Create Date: 2026-08-13

**`m09e` made the neighbour rebuild 16x slower and the width is only 2.67x of
it. This is the rest.** Measured on the real 130,720-row catalog: the shipped
`TitleEmbeddingRepository.nearest_for` ran at **36.50 ms/seed at halfvec(384)**
and **594.7 ms/seed at halfvec(1024)** -- a full `usher similar --rebuild` going
from 80 minutes to 21.6 hours. A wider vector explains 2.67x of that. The other
6.1x is TOAST.

## The mechanism, which is a threshold and not a slope

pgvector declares `halfvec` with storage **EXTERNAL**, so a value goes
out-of-line once the tuple exceeds `TOAST_TUPLE_THRESHOLD` -- **2,032 bytes**.

| lanes | `pg_column_size` | inline? |
|---|---|---|
| 384 | 772 | **yes** |
| 1024 | **2,052** | **no** |

So the same declaration produced inline storage at 384 and out-of-line storage
at 1024, and nothing in `m09e` mentioned it because nothing in `m09e` knew. The
observable, measured on the live table before this revision:

    title_embeddings   main heap 17 MB   TOAST heap 340 MB

Seventeen megabytes of row headers pointing at three hundred and forty
megabytes of vectors. Every distance computation in an exact scan then costs a
TOAST index descent plus a heap fetch, **per row, per seed**: `EXPLAIN (ANALYZE,
BUFFERS)` over ten seeds read **10,061,071** buffers against a 90,000-page
table, an **11x amplification**, where the same query over an inline copy read
**523,005**.

## What it buys, measured rather than projected

A copy of the real table with `SET STORAGE PLAIN`, same 130,720 rows, same
seeds, same forced-exact-scan GUCs, two runs each, with an unrelated rebuild
contending equally against both:

| storage | ms/seed | full 130,720-seed walk |
|---|---|---|
| EXTERNAL (as `m09e` left it) | 603.4 / 598.1 | **21.6 h** |
| PLAIN | **110.3 / 110.0** | **4.0 h** |

**5.4x.** And 110 ms/seed against the 384-lane era's 36.50 is **3.0x**, which is
the 2.67x the lane count predicts plus change -- so with TOAST gone the cost is
linear in the width again, which is the check that says the diagnosis is
complete rather than merely helpful.

## Three columns, and one of them has been paying this since M7

`genome_scores.relevance` is `halfvec(1128)` = **2,260 bytes**, over the same
threshold, and it shipped in `ffa`. Measured today: **1,544 kB of heap against
41 MB of TOAST**. Every genome similarity this project has ever measured was
measured through a TOAST fetch. Nothing is known to be wrong with those
numbers; they were simply never taken any other way, and `db/models/taste.py`
records the value's size (*"1,128 halfvec lanes is 2,256 bytes plus a header"*)
without drawing the conclusion, which is what makes this worth a paragraph
rather than a line.

`user_taste.centroid` is `halfvec(1024)` and empty at this revision, so it costs
nothing to fix and would have cost the same as `title_embeddings` the moment a
household got a centroid.

## The mechanism this uses, and the two that do not work

**`SET STORAGE` alone changes nothing that already exists** -- it applies to
rows written after it. Two obvious rewrites were tried on a 2,000-row probe and
neither works:

- **`UPDATE t SET v = v`** keeps the existing TOAST pointer rather than
  re-storing the value. The heap doubled with dead tuples and the TOAST relation
  did not shrink.
- **`ALTER COLUMN v TYPE halfvec(1024) USING v`** does not rewrite (the type is
  unchanged) **and resets `attstorage` back to the type's default `e`**, quietly
  undoing the `SET STORAGE` above it. Verified: `storage=p` before, `storage=e`
  after.

What works is `SET STORAGE PLAIN` followed by `VACUUM FULL`, verified on the
same probe: `attstorage` `e` -> `p`, `reltoastrelid` **1305036 -> 0** (the TOAST
relation is dropped outright), heap 104 kB -> 5.46 MB, and every value read back
intact at 2,056 bytes.

**`VACUUM FULL` cannot run inside a transaction block**, which is why the body
below opens an `autocommit_block()`. It takes an `ACCESS EXCLUSIVE` lock and
rebuilds the HNSW index with the table, so this revision is not online -- run it
when nothing is serving.

## The ceiling this introduces, which is the cost side

**`PLAIN` forbids out-of-line storage outright, so a value that does not fit in
a page makes the INSERT fail rather than spill.** A `halfvec` is `8 + 2 * dim`
bytes and a page has ~8,100 usable, so this caps `EMBEDDING_DIMENSIONS` at
roughly **4,000 lanes**. That is above pgvector's own HNSW ceiling for
`halfvec` and far above the 1024 shipped here, but it is a real bound and it is
recorded on `EMBEDDING_DIMENSIONS` as well as here: a future model wider than
~4,000 needs `MAIN` (inline where it fits, out-of-line where it does not) rather
than `PLAIN`. **`MAIN` was not measured.** It would also keep these vectors
inline and it attempts compression on every write, which is wasted work on
float data; `PLAIN` is what the 5.4x above was measured with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "m09f"
down_revision: str | Sequence[str] | None = "m09e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every `halfvec` column in the schema, checked against `pg_type` rather than
#: remembered: `title_embeddings.embedding` and `user_taste.centroid` from
#: `m09e`, `genome_scores.relevance` from `ffa`. If a fourth is ever added it
#: belongs here, and `test_every_halfvec_column_stores_inline` is what fails if
#: it is not.
_VECTOR_COLUMNS = (
    ("title_embeddings", "embedding"),
    ("user_taste", "centroid"),
    ("genome_scores", "relevance"),
)

#: pgvector's declared default for `halfvec`, which is what `downgrade()`
#: restores. Read off `pg_attribute.attstorage` rather than assumed.
_TYPE_DEFAULT = "EXTERNAL"


def _set_storage(mode: str) -> None:
    """Set the storage mode on every vector column and rewrite each table.

    The `VACUUM FULL` is what moves values that already exist; without it this
    migration changes only how future rows are written, which is the failure
    mode that would make it look applied and measure unchanged.
    """
    for table, column in _VECTOR_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STORAGE {mode}")
    # Outside the migration's transaction, because `VACUUM` refuses to run in
    # one. Each table separately rather than a bare `VACUUM FULL`: this touches
    # three relations and a database-wide rewrite would take every other table
    # with it, including the 1.27M-row `titles`.
    with op.get_context().autocommit_block():
        for table, _ in _VECTOR_COLUMNS:
            op.execute(f"VACUUM FULL {table}")


def upgrade() -> None:
    _set_storage("PLAIN")


def downgrade() -> None:
    _set_storage(_TYPE_DEFAULT)
