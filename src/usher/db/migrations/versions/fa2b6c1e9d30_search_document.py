"""the search document: extensions, an immutable wrapper, a generated column

Revision ID: fa2b6c1e9d30
Revises: f1a7d3c9e824
Create Date: 2026-08-02

**The revision-id convention restarts here, and this is the note that says
so.** Existing ids spell `a8a0...`, `b3f1...`, `c7a2...`, `d4c9...`,
`e5b8...`, `f1a7...` in chain order -- one hex letter per migration, and the
letter after `f` is not hex. M6 continues with a second cycle: `fa...`,
`fb...`, `fc...`. Each sorts after `f1a7d3c9e824` under a plain `ls`, and
the second character carries the sequence. Do not re-derive this; extend it.

**This is the first migration in this repository that creates an
extension**, and it creates three: `vector` (M6's `halfvec` column, added by
fb4e0a7d2c15), `pg_trgm` (the type-ahead path's trigram index) and
`fuzzystrmatch` (`levenshtein_less_equal`, the type-ahead re-rank). All three
are *available* in `pgvector/pgvector:pg17` -- the image
`tests/integration/conftest.py` already uses -- and not *installed*, so
something has to install them and this is the something.

**They are created `IF NOT EXISTS` and they are never dropped, and that
asymmetry is deliberate.**

- An extension is a *database-wide* fact, not a schema object this migration
  owns. Another application sharing this database may already have installed
  `pg_trgm`; `IF NOT EXISTS` makes that not an error, and not dropping means
  a downgrade does not take a capability away from somebody else.
- `DROP EXTENSION vector` **fails** while any object depends on it. After
  fb4e0a7d2c15 that includes `title_embeddings.embedding`, so a downgrade of
  this migration would have to be ordered against a migration it knows
  nothing about.
- `DROP EXTENSION vector CASCADE` is the version that "works", and it works
  by silently dropping the dependent columns. A downgrade that destroys data
  without saying so is what ADR-0010 exists to refuse, one layer down.

The cost, stated rather than glossed: `alembic downgrade base` no longer
returns the database byte-for-byte to its original state. Nothing observes
that -- `test_migration_matches_the_orm_metadata` diffs tables, indexes and
columns, not extensions -- so it is a documented residue, not a drift the
suite will trip over. `CREATE EXTENSION` also needs a role permitted to run
it; in the compose stack and in the test container that is the role
`POSTGRES_USER` creates, which owns the database.

**`usher_array_text` exists because PRD 05's expression does not compile.**
`GENERATED ALWAYS AS (...) STORED` rejects it with `ERROR: generation
expression is not immutable`, and the cause is exactly one function:
`array_to_string(anyarray, text)` is `STABLE`, not `IMMUTABLE`, because
`anyarray` admits element types whose output depends on a GUC (`timestamptz`
and `TimeZone`). Verified from `pg_proc`: `to_tsvector(regconfig, text)` is
`IMMUTABLE` -- so the explicit `'english'` is load-bearing and a bare
`to_tsvector(text)` would **not** work -- and `setweight` is `IMMUTABLE`.

`array_to_tsvector` is immutable, is the obvious core-function fix, and is
**wrong for this purpose**: it emits array elements as raw, unlexized,
case-preserving lexemes. Measured on `ARRAY['Sci-Fi','Film-Noir','Drama']`
it produces `'Drama' 'Film-Noir' 'Sci-Fi'`, which fails to match even
`websearch_to_tsquery('english','drama')`. Rejected on evidence, and
recorded here so it is not "fixed" back in later.

**CHANGING THE WRAPPER'S BODY REQUIRES A FORCED REWRITE IN THE SAME
MIGRATION.** `CREATE OR REPLACE FUNCTION` does not recompute stored
generated values -- verified directly: a row stored as `'alpha':1 'beta':2`
did not move when the body changed, while a fresh evaluation returned
something else, and a subsequent `UPDATE` of that row *did* recompute it. So
a replaced body produces a table where some rows were computed by the old
definition and some by the new, with nothing to tell them apart and the
split decided by which rows happened to be touched since. The recipe, in
this order, in one migration:

    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    op.execute("CREATE OR REPLACE FUNCTION usher_array_text ...")
    op.execute("ALTER TABLE titles ADD COLUMN search_document ... STORED")
    op.create_index("ix_titles_search_document", ...)

Drop-and-re-add rather than `UPDATE titles SET id = id`: both force a full
rewrite, but the update also leaves 1.27M dead tuples and their WAL behind
for autovacuum. Either way it is a table rewrite on the whole catalog, so it
is a maintenance window and not a hot deploy.
`tests/integration/test_search_document.py::
test_the_stored_document_equals_a_freshly_computed_one` is what catches a
migration that forgets.

**Cost, measured 2026-08-02 against `pgvector/pgvector:pg17` on 300,000
synthetic skeleton-shaped rows through `INSERT ... SELECT`** -- the
bootstrap's own statement shape:

    INSERT ... SELECT 300k    734 ms -> 2,980 ms   (4.06x)
    total relation size        57 MB -> 76 MB      (+33%)

Extrapolated to 1,271,138 titles: about +9.5 seconds and about +80 MB on the
whole-catalog bootstrap. Against a bootstrap already measured in minutes
that is noise, and it buys a freshness guarantee no amount of code can
equal. Two costs are **not** in that figure and are not measured: the GIN
index's own write cost, and `apply_ratings`' `UPDATE` over 538,937 rows,
each of which recomputes a `tsvector`.

**`fastupdate = off` on the GIN index, and it is native Alembic** --
verified by compiling the DDL. The default pending list defers index
maintenance into an unsorted list that every query then scans linearly until
autovacuum flushes it, which is precisely wrong for a table written in
million-row bursts and queried during them. Verified with `pageinspect`:
after 5,000 inserts, `fastupdate = off` had `n_pending_pages = 0 /
n_pending_tuples = 0` against `50 / 5000` for the default. The read side is
the sharper argument and is also measured: a 1.6 MB pending list cost 231
buffers against 30 -- 7.7x read amplification on the index stage, invisible
in `EXPLAIN` unless you look at buffers.

**The trigram index is GIN, not the GiST PRD 05 specifies -- and that is a
decision under an open question rather than a settled win.** At 300k rows
GIN beat GiST on every axis measured: build 579 ms against 1,965 ms, index
size 7,968 kB against 22 MB, p50 lookup 9.01 ms against 21.1 ms.

**A later 2.08M-name run reopened half of it, and the honest summary is that
GIN and GiST answer two different questions.** On the `%` threshold path GIN
is ~110x faster (1.671 ms / 205 buffers against 182.5 ms / 31,174), builds in
7.5 s against 23.1 s, and is 69 MB against 244 MB -- so for "which rows are
candidates", GIN is not close. **But GIN has no KNN operator class at all**:
`ORDER BY name <-> q` cannot use it and degrades to a Seq Scan at 3,989.9 ms,
where GiST answers the same query from the index. On a 604-case
single-edit-typo dry run over short titles, GiST KNN and GIN `%` @0.1 tied on
recall@5 at 93.5%, and GiST had the better tail (p50 281 ms / p95 342 ms
against 582 ms / 1,893 ms).

So: this migration ships GIN because the capped-candidate path is the one
`PostgresSuggestIndex` is designed around, and a cap is exactly what removes
GIN's only exposure -- collecting every match before the top-N sort. **The
KNN question is not closed by this migration**, and if the suggest path ever
needs `ORDER BY name <-> q` it needs a GiST index, not a tuning change. That
call belongs to the task that measures the type-ahead path end to end, with
both numbers in front of it.

Note also that no plan-shape test can distinguish the two -- GiST serves `%`
as well -- so a green suite is not evidence for this choice. The
measurements are.

**On `name` raw, not on `lower(name)`.** `pg_trgm` folds case while
generating trigrams, so `%` and `similarity()` are already case-insensitive.
`ix_titles_name_lower_year` right beside it *is* an expression index on
`lower(name)`, for a btree equality lookup where case does matter -- the two
look inconsistent and are not.
`tests/integration/test_trigram_index.py::
test_pg_trgm_folds_case_so_the_index_is_on_the_raw_column` verifies the
premise rather than trusting it.

**`original_name` deliberately gets no index.** An extra GIN index on
`titles` is paid on every write forever, including the 1.27M-row bootstrap;
two predicates turn a single capped index scan into an unmeasured `BitmapOr`
with undefined cap semantics; and a hit found only by original name renders
under a canonical name the user did not type. The measurements that would
settle it -- the fraction of the real catalog whose `original_name` differs
from `name`, and suggest recall@5 with and without -- belong to the
real-catalog gate, where a recall number means something.

**There is no `title_search_names` table in this revision** (boundary call
3). PRD 05's narrow table is justified by aliases and people names, neither
of which has a data source in M6, so it would hold exactly one row per title
duplicating `titles(id, name, kind, popularity)` -- a second copy to keep
fresh, which is the problem this milestone exists to eliminate.

**`m09a` creates it**, once both halves have a source, and the duplication
this paragraph refuses is still refused there: it carries no `primary` rows
and no `popularity` column, so `titles` remains the only place a canonical
name is stored. The condition was restated by M7 rather than silently
renewed -- M7 landed people and not aliases -- which is why this sentence
names a revision and not a milestone.

Reversible. The downgrade drops both indexes, the column and the function,
and leaves the three extensions installed -- see above.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "fa2b6c1e9d30"
down_revision: str | Sequence[str] | None = "f1a7d3c9e824"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTENSIONS = ("vector", "pg_trgm", "fuzzystrmatch")

# IMMUTABLE is a promise the planner takes on trust. It is honest here and
# only here: array_to_string is STABLE because `anyarray` admits element
# types whose output depends on a GUC (timestamptz and TimeZone), and text
# has no such dependency. Narrowing the signature to text[] is what makes the
# promise true -- do NOT widen it to anyarray to "reuse" it.
_WRAPPER = """
CREATE FUNCTION usher_array_text(text[]) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT array_to_string($1, ' ') $$
"""

# Verified populated and weighted: a row named `Iron` with genres
# {autumn,winter} and no other populated input stores
# `'autumn':2 'iron':1A 'winter':3`. Weight `D` is the tsvector default and
# is not printed -- worth knowing before someone reads its absence as a bug.
# Positions are not per-field: `tsvector || tsvector` shifts the right
# operand's positions past the left operand's maximum, so a populated
# overview moves every later lexeme along.
_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   -- Weight B is reserved for cast and crew and is deliberately absent
   -- rather than filled with something else (boundary call 2). There is no
   -- Person/Credit table, model or port anywhere in src/; ports/metadata.py
   -- defers all of it to M7 by name. When M7 lands Credit, filling this is a
   -- migration plus one forced rewrite, not a redesign.
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""


def upgrade() -> None:
    for extension in _EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    op.execute(_WRAPPER)
    op.execute(_COLUMN)
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )
    op.create_index(
        "ix_titles_name_trgm",
        "titles",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_titles_name_trgm", table_name="titles")
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    # After the column is gone nothing depends on the wrapper, so this is a
    # plain DROP rather than a CASCADE. The three extensions stay -- see the
    # module docstring.
    op.execute("DROP FUNCTION usher_array_text(text[])")
