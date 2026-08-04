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

**This migration is half-written on purpose between two commits.** The column
lands here; the forced rewrite of `search_document` that reads it extends this
same `upgrade()` in the commit that fills weight class B. Splitting the two
into separate revisions would spend a migration from the budget and would
leave a `credit_names` column that exists for one revision without being read
by the document -- a half-migrated tier with nothing to tell the halves apart.
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


def upgrade() -> None:
    # The column first, populated to '{}' for every existing row by the
    # server_default. The commit that fills weight class B extends this
    # function with the forced rewrite of search_document that reads it;
    # until then the column exists and nothing references it, which is a
    # state this migration is never applied in outside the branch.
    op.add_column(
        "titles",
        sa.Column(
            "credit_names",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("titles", "credit_names")
