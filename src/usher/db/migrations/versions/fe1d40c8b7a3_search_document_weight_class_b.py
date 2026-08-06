"""titles.credit_names, and weight class B

Revision ID: fe1d40c8b7a3
Revises: fd7c3a5b9e12

**A stored generated column cannot reach another table, and Postgres refuses
two of the three ways of trying. It accepts the third in silence, and the
third is the one a competent implementer reaches for.** Measured 2026-08-04
on `pgvector/pgvector:pg17` -- **PostgreSQL 17.10 (Debian 17.10-1.pgdg12+1)**
-- against a two-table schema shaped like `titles` and `credits`. The error
text is recorded verbatim because it is *not* the error boundary call 5
implies, and `fa2b6c1e9d30` has trained everyone on this schema to expect
`ERROR: generation expression is not immutable`.

Spelling 1, the subquery::

    ALTER TABLE titles ADD COLUMN search_document tsvector
    GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,'')), 'A')
      || setweight(to_tsvector('english',
           (SELECT string_agg(c.name, ' ') FROM credits c
            WHERE c.title_id = titles.id)), 'B')
    ) STORED;

    ERROR:  cannot use subquery in column generation expression

Postgres never gets as far as asking about volatility: the subquery is
refused syntactically first. The **uncorrelated** form fails identically --
`setweight(to_tsvector('english', (SELECT 'x')), 'B')` is also `cannot use
subquery in column generation expression` -- so it is genuinely about
subqueries and not about the join.

Spelling 2, the bare cross-table reference::

    ALTER TABLE titles ADD COLUMN sd3 tsvector
    GENERATED ALWAYS AS ( to_tsvector('english', credits.name) ) STORED;

    ERROR:  missing FROM-clause entry for table "credits"

Also loud, also fine.

Spelling 3, the "obvious fix" -- **and it raises nothing**::

    CREATE FUNCTION credit_names_of(int) RETURNS text LANGUAGE sql IMMUTABLE
      AS $$ SELECT coalesce(string_agg(name,' '),'') FROM credits
            WHERE title_id = $1 $$;

    ALTER TABLE titles ADD COLUMN sd4 tsvector
    GENERATED ALWAYS AS (
       setweight(to_tsvector('english', coalesce(name,'')), 'A')
       || setweight(to_tsvector('english', credit_names_of(id)), 'B')
    ) STORED;

    ALTER TABLE

`IMMUTABLE` is a promise the planner takes on trust -- `fa2b6c1e9d30` says so
in its own words about `usher_array_text` -- and a function that reads a
table can make that promise and be believed. What the column then does,
measured step by step:

1. credit inserted, **then** the title row ->
   ``'autumn':1A 'iron':2A 'marlow':3B 'vanc':4B`` -- correct.
2. title row inserted, **then** the credit ->
   ``'nine':1A 'harbour':2A`` -- the credit is missing.
3. ...then an unrelated ``UPDATE titles SET name = ...`` ->
   ``'ten':1A 'harbour':2A 'iri':3B 'kemp':4B`` -- it appears.
4. a credit ``DELETE``d from ``credits`` ->
   the name **stays in the document**, permanently.

So the table ends up with some rows reflecting current credits and some
reflecting credits as of whenever that row was last touched, with nothing to
tell them apart and the split decided by which rows happened to be written
since. That is exactly `fa2b6c1e9d30`'s `CREATE OR REPLACE FUNCTION`
mixed-state failure arriving through a different door, with **no migration to
blame and no forced rewrite that could fix it**, because the drift is
continuous rather than one-off. A trigger is not the only design failure
available here; the lying wrapper is closer to hand and is worse, because a
trigger at least fires on the credit write.

Hence the denormalised column. `credit_names` is `credits` projected to names
and truncated to the top billed, maintained by the one statement that also
writes `credits` (`DeriveService`), and read by the generated expression in
its own row.

**NOT NULL with a server_default, and the third reason is measured.**
`usher_array_text` is declared `STRICT`, so `usher_array_text(NULL)` is NULL
and `tsvector || NULL` is NULL -- the *entire* document, name included. On
pg17.10 against this schema's own wrapper, a row with a populated `name` and
`credit_names IS NULL` stored `search_document IS NULL`, while the same row
with `'{}'` stored `'harbour':2A 'iron':1A`. The title disappears from every
full-text query and from `ix_titles_search_document`, and nothing raises.

**No backfill from `credits`.** `DeriveService` is the only correct writer and
`usher derive --backfill` is the command that runs it; a migration that
populated the array would be a second writer with its own copy of the top-ten
rule, and the two would disagree the moment that cutoff moves.

**The forced rewrite, and it is four of `fa2b6c1e9d30`'s five steps.** That
migration's docstring records the measurement: `CREATE OR REPLACE FUNCTION`
does **not** recompute stored generated values -- a row stored as
`'alpha':1 'beta':2` did not move when the body changed, while a fresh
evaluation returned something else, and a subsequent `UPDATE` of that row
*did* recompute it. So a changed expression left in place produces a table
where some rows were computed by the old definition and some by the new, with
nothing to tell them apart and the split decided by which rows happened to be
touched since. The recipe is drop index, drop column, replace function, re-add
column, recreate index.

**`usher_array_text` is not replaced here, and the omission is stated so a
reader who has been told the recipe is five steps and finds four does not
assume a mistake.** The wrapper's body is unchanged; what changed is the
*expression* that calls it. Four steps, in order, in one migration.

`tests/integration/test_search_document.py::test_the_stored_document_equals_a_freshly_computed_one`
is what catches a migration that forgets, and it is visible only against a
seeded database -- a fresh `upgrade` on an empty table has no stale rows to
disagree with.

**One measured alternative, recorded so it is not rediscovered as a fix.**
PostgreSQL 17 has `ALTER TABLE ... ALTER COLUMN ... SET EXPRESSION AS (...)`,
and it does everything this needs in one statement: it rewrites the table,
recomputes every row under the new expression, preserves the GIN index
including its `WITH (fastupdate=off)` reloption, and leaves
`count(*) WHERE doc IS DISTINCT FROM (<fresh expression>)` at zero. PRD 01
pins the stack at PostgreSQL 17, so it is available. **Declined**, for one
reason: the drop-and-re-add recipe is already written down, already reversible
in `fa2b6c1e9d30`'s `downgrade()`, and works for the *other* change this schema
will eventually make -- a wrapper **body** change, which `SET EXPRESSION` does
not help with, because the function must be replaced first and every row
recomputed after. Two recipes for one hazard is how the wrong one gets used on
the case it does not cover.

**The intended blast radius: every fingerprint in the enriched tier moves at
once, so the whole tier re-embeds.** Including titles with no credits, and the
reason is the positional assembly -- an uncredited title gains a seventh
*empty* segment, which is one more `CHR(10)` in the hashed string. There is no
subset of the catalog that keeps its old fingerprint. That is ADR-0020's
scheme working rather than a bug, and `services/search.py` already priced it:
*"Changing the assembly invalidates every stored vector, on purpose... That is
the scheme working, not a migration to write."* 25 s to 2 min at the measured
throughput (~8,000-10,700 tokens/s on CPU, ~100-130 tokens a document) over
the 2k-10k titles boundary call 4 embeds.

**Operator sequence after this lands**, and the order matters: `alembic
upgrade head` -> `usher derive --backfill` -> `usher index --backfill` ->
`usher work`. Indexing before deriving embeds every title with an empty class
B and then re-claims all of them once `credit_names` is populated, which is
the wasted pass twice over. It is also a **table rewrite on the whole
catalog**, so it is a maintenance window and not a hot deploy.

**This migration was half-written on purpose between two commits.** The column
landed first; this rewrite extends the same `upgrade()`. Splitting the two into
separate revisions would spend a migration from the budget and would leave a
`credit_names` column that exists for one revision without being read by the
document -- a half-migrated tier with nothing to tell the halves apart.
Legitimate because this file reaches `head` only when `milestone/m7-rows`
merges, so it is never applied to a long-lived database in the intermediate
state.

**The revision id extends by a character rather than starting a third cycle.**
`fa2b6c1e9d30`'s convention fixes one hex character per migration and M6's
second cycle ended at `fa`/`fb`/`fc`; `fd` is Group B's and this is `fe`. A
third cycle beginning with a digit would sort *before* `fa`, so the remedy is
`ff` -> `ffa` -> `ffb`, which stays correctly ordered and is unbounded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fe1d40c8b7a3"
down_revision: str | Sequence[str] | None = "fd7c3a5b9e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Weight class B fills the slot fa2b6c1e9d30 reserved for it -- "reserved for
# cast and crew and deliberately absent rather than filled with something
# else" -- and slots between A and C, which is where ts_rank's default weight
# vector {0.1, 0.2, 0.4, 1.0} puts it. Measured on pg17.10 over three rows
# carrying one term in three classes, scored with
# ts_rank(doc, websearch_to_tsquery('english', 'marlow vance')):
# name 0.9910322 (A), credit_names 0.39641288 (B), overview 0.19820644 (C).
# A is 2.5x B and B is 2x C, with no ties and no tuning.
_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   || setweight(to_tsvector('english', usher_array_text(credit_names)), 'B')
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""

# fa2b6c1e9d30's expression, restated rather than imported: a migration must
# not import application code that can change under it, and `downgrade()` has
# to restore the definition that revision installed rather than whatever the
# model happens to say today.
_M6_COLUMN = """
ALTER TABLE titles ADD COLUMN search_document tsvector
GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(name,          '')), 'A')
   || setweight(to_tsvector('english', coalesce(original_name, '')), 'A')
   || setweight(to_tsvector('english', coalesce(overview,      '')), 'C')
   || setweight(to_tsvector('english', coalesce(tagline,       '')), 'C')
   || setweight(to_tsvector('english', usher_array_text(genres)),    'D')
   || setweight(to_tsvector('english', usher_array_text(keywords)),  'D')
) STORED
"""


def upgrade() -> None:
    # The column first, populated to '{}' for every existing row by the
    # server_default, so the expression below has something non-NULL to read
    # the moment it exists. `usher_array_text` is STRICT: a NULL here would
    # null the whole document.
    op.add_column(
        "titles",
        sa.Column(
            "credit_names",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Then the forced rewrite. Drop-and-re-add rather than
    # `UPDATE titles SET id = id`: both force a full rewrite, but the update
    # also leaves 1.27M dead tuples and their WAL behind for autovacuum.
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    # (fa2b6c1e9d30's third step, CREATE OR REPLACE FUNCTION, is deliberately
    # absent -- the wrapper's body is unchanged. See the docstring.)
    op.execute(_COLUMN)
    # `fastupdate=off` is carried across the rewrite deliberately and is
    # spelled out at both call sites rather than shared through a dict: its
    # default pending list cost a measured 231 buffers against 30 -- 7.7x
    # read amplification, invisible in EXPLAIN unless you look at buffers --
    # and an index recreated without it loses that silently.
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )


def downgrade() -> None:
    # The mirror order, and it is a forced rewrite in this direction too: a
    # downgrade that dropped only the column would leave every stored document
    # computed under the class-B expression while the schema claimed the M6
    # one. head -> base -> head in tests/integration/test_migrations.py is what
    # exercises it.
    op.drop_index("ix_titles_search_document", table_name="titles")
    op.execute("ALTER TABLE titles DROP COLUMN search_document")
    op.drop_column("titles", "credit_names")
    op.execute(_M6_COLUMN)
    # `fastupdate=off` is carried across the rewrite deliberately and is
    # spelled out at both call sites rather than shared through a dict: its
    # default pending list cost a measured 231 buffers against 30 -- 7.7x
    # read amplification, invisible in EXPLAIN unless you look at buffers --
    # and an index recreated without it loses that silently.
    op.create_index(
        "ix_titles_search_document",
        "titles",
        ["search_document"],
        postgresql_using="gin",
        postgresql_with={"fastupdate": "off"},
    )
