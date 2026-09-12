"""`search_queries` learns which surface asked, and three indexes land.

Revision ID: m10c
Revises: m10b
Create Date: 2026-08-26

PRD 10's `## Analytics tables` block names two amendments and refuses to pick
one; this is amendment 2, *"a tenth column (`surface`, `search | suggest`, or a
nullable `tier`)"*, taken because it is the only one of the two that can record
*which tier* answered -- the half ADR-0031 actually wants measured -- and
because amendment 1 would put a fourth `SearchMode` member on `GET /search`'s
`?mode=` where no search lane can serve it.

**The slug is `m10c` and not `m10a`.** The M10 plan reserved `m10a` for this
revision on 2026-08-13, when head was `m09f`; `origin/main` has since landed
`m10a` (the rating-provenance split, ADR-0040) and `m10b` (issue #41's
`sync_runs.position`). Head measured by walking `ScriptDirectory` rather than by
reading the versions directory: `get_heads() == ['m10b']`, exactly one.

## Two columns, and `server_default` is refused with its reason

`m09d_people_provenance.py`'s three statements, in its exact spelling: *"Three
statements, and the order is the point: nullable, backfilled, then NOT NULL.
Never `server_default`, which would outlive this migration and supply a
plausible wrong value to a writer that forgot."*

`surface` follows it verbatim, and `'search'` is the **true** value for every
row that exists rather than a convenient one: `SearchService._record_search` has
exactly one call site (`SearchService.search`), which has exactly two callers --
`GET /search` and `usher search` -- and `SearchService.suggest` writes nothing
at this revision. So the backfill states a fact about the rows rather than
guessing at one.

`tier` is **nullable and stays nullable**: a `search` row has no tier, and PRD
10's amendment spells it *"a nullable `tier`"*. A `NOT NULL` here would need a
sentinel member, which is a third vocabulary entry meaning "not applicable" in a
column whose whole purpose is to keep two vocabularies apart.

**Both are `VARCHAR`, not a Postgres enum and not a CHECK**, and the precedent
is `search_queries.mode` one field over. `usher.db.base.enum_column` compiles
`native_enum=False` to `VARCHAR(length)` with `values_callable` binding each
member's `.value`, and `create_constraint` defaults to `False` in SQLAlchemy
2.0 -- so no membership CHECK is emitted either; Pydantic owns membership,
matching every other constraint in this schema. There is no `CREATE TYPE`
anywhere in this database and this revision adds none, which is also why
`downgrade()` has no type to drop.

## Cost

`ADD COLUMN` with no default has not rewritten a table since PostgreSQL 11, and
`ALTER TABLE ... SET NOT NULL` scans it once under `ACCESS EXCLUSIVE`. That is
`m09d`'s *"Land the column before the volume"* argument and the reason this
revision is not deferred until the suggest writer has filled the table:
`search_queries` holds **107 rows in 72 kB** on this deployment (measured
read-only 2026-08-26), and the scan is instant now and is not later.

**`m09f`'s storage lesson does not apply here, and it is priced rather than
waved through.** That finding is about `halfvec` and a threshold: 1024 lanes is
2,052 bytes against `TOAST_TUPLE_THRESHOLD`'s 2,032, and crossing it cost 6.1x
on a whole rebuild. Neither column here is a vector. Measured on the live table
2026-08-26, a `search_queries` row is **avg 110.2 bytes, max 149 bytes**,
longest `query` **57 characters**; `surface` and `tier` add at most 15 bytes
including varlena headers, leaving the row an order of magnitude below the
threshold. The one column that could ever TOAST this table is `query`, which is
`text` bounded only by `ck_search_queries_query_not_empty`, and that is true at
`m10b` independently of this revision. No `SET STORAGE` is needed and none is
added.

## Three indexes, one of which has a reader and two of which do not yet

`ix_search_queries_at` serves PRD 10's own pruning statement, `DELETE FROM
search_queries WHERE at < now() - interval '90 days'`, which that document
records as a sequential scan *"until somebody adds one"*. Measured on the live
table 2026-08-26: `pg_indexes` lists exactly `pk_search_queries`, so the claim
was true as written and is discharged here rather than corrected.

**A btree, and BRIN was considered and refused.** `at` is append-only and
physically correlated (UUIDv7 primary key, inserts in time order), which is the
shape BRIN is for -- but `record_outcome`'s `UPDATE` writes a new heap tuple for
every row that gains a click or a play, and whether those stay on their original
page is a `fillfactor` question nobody in this project has measured. A btree also
matches `ix_llm_calls_at` below, which is the same shape for the same
`WHERE at >= :since` query family, and on a table measured at 72 kB the index
size saving BRIN exists for is worth nothing.

**What justifies it is the steady state, not the present, and the arithmetic
is the argument.** A daily prune at a 90-day window matches about **1/90th** of
the table -- **1.1%** -- which is the selectivity ratio at which a btree beats a
sequential scan, and precisely the ratio a *first* run after a long outage does
not have. Without the index every daily prune reads 100% of the relation to
delete 1.1% of it; with it, it reads the 1.1%. **On this deployment today the
index buys nothing measurable and that is stated rather than dressed up**:
`search_queries` held **9 rows in 32 kB** when this milestone was planned
(2026-08-13), and the 90-day `DELETE` was a Seq Scan of **1 shared buffer, 0
rows removed, 0.043 ms**. The volume the index exists for is the suggest
writer's, which arrives at one row per keystroke rather than one per press of
enter. ⚠️ **The 1.1% argument is about the *selection*, not the whole
statement** -- measured on the 14,978-row clone, the planner still joins the
selected ids back through a sequential scan at that size (PRD 10 records it),
so the ratio governs which access path finds the rows and not the cost of
removing them.

**The two `llm_calls` indexes ship here with no reader, deliberately, and
`m08a`'s objection is answered with a number rather than overruled.** They are
copied from `m08a_curation.py`'s own docstring rather than re-derived, partial
predicate included. `m08a` refused them because *"an index nothing reads is
`ix_titles_popularity` again: maintained on every write, for a consumer that
does not exist"*. `llm_calls` holds **0 rows and 16 kB** on this deployment
(measured read-only 2026-08-26, unchanged from the 2026-08-13 reading) and
gains one row per generation per household per night thereafter, so the
maintenance cost of both is bounded by the curation cadence and is not
measurable. They ship *here* because this milestone gets one revision: a reader
task in another group authoring its own DDL would be a second head, and a
pre-allocated chain is a serial spine across every group holding a link in it
(`db-and-sql.md`, *"Allocate a revision id per merge, never per author"*).
**Nothing in this revision reads them**, and
`test_the_cost_ledger_has_no_read_method` was still true after it. ⚠️ **That
case no longer exists**: M10's D3 added `LLMCallRepository.list_since` --
`WHERE at >= :since ORDER BY at`, which is `ix_llm_calls_at`'s own query -- and
deleted the guard in the same commit, on the exit condition the guard set for
itself. The surface is now pinned by `tests/unit/test_ports.py`'s parametrised
entry at `{"record", "list_since"}`. This paragraph is left standing because it
is what was true at this revision; the pointer is here so the name does not
dangle.

## `downgrade()` mirrors `upgrade()` statement for statement

**The three `DROP INDEX` lines here are load-bearing**, unlike `m08a`'s -- which
preceded a `drop_table` that took the index anyway and says so in its own
comment. Nothing else removes these: a `downgrade()` that dropped only the two
columns would leave `ix_llm_calls_at` and `ix_llm_calls_generation_id` behind on
a table that still exists, and `ix_search_queries_at` on a table that still
exists too. The two spellings look alike, which is why this paragraph is here.

🔴 **This revision is reversible in its *schema* and destructive in its
*data*, and the second half is stated here because everything else about it
reads reversible.** `downgrade()` drops `surface` and `tier` from a table it
does not drop, so a `search_queries` row survives the cycle with both facts
gone, and `upgrade()` re-applied answers `('search', NULL)` for every one of
them. On a deployment where the keystroke writer has run
(`USHER_SEARCH_SUGGEST_ANALYTICS`) **a down-then-up cycle silently relabels every
`surface = 'suggest'` row as `'search'` and discards the tier that answered
it**, which is exactly the two-vocabularies confusion this column was added to
prevent, arriving from the migration rather than from a writer.

**The backfill's missing `WHERE` is not the mechanism and adding one would be
theatre.** `UPDATE search_queries SET surface = 'search'` has no predicate, and
a reviewer reading it reaches for `WHERE surface IS NULL`; that guard can never
match differently, because the column does not exist when `upgrade()` runs --
`downgrade()` dropped it, so every row is NULL by construction on the second
application exactly as on the first. Writing it would move a reader's eye off
`drop_column`, which is where the data actually goes, onto a predicate that
cannot fire. There is nowhere to put the values: a column-adding migration's
inverse is dropping the column, and this schema has no side table to park them
in. So the destruction is recorded rather than repaired, and it is *measured*
rather than recorded -- `tests/integration/test_m10_schema.py::
test_a_down_and_up_cycle_relabels_a_suggest_row_and_the_artefact_check_cannot_see_it`
drives the cycle over a real row and asserts the relabelling, because the
five-artefact round trip beside it asserts *presence* and never data, and a
paragraph nothing runs is how this claim would go stale.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10c"
down_revision: str | Sequence[str] | None = "m10b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Three statements, and the order is the point: nullable, backfilled, then
    # NOT NULL. Never `server_default` -- it would outlive this migration and
    # supply a plausible wrong value to a writer that forgot. `m09d`'s spelling.
    #
    # **The `UPDATE` has no `WHERE` and must not grow one** -- see the module
    # docstring. The statement below runs only against a table where this
    # column has just been added, so every row is NULL and any predicate
    # matches all of them; a `WHERE surface IS NULL` here would read as a
    # guard against re-application and would guard nothing, while pointing
    # away from `downgrade()`'s `drop_column`, which is what actually
    # discards a `'suggest'` label.
    op.add_column("search_queries", sa.Column("surface", sa.String(length=8), nullable=True))
    op.execute("UPDATE search_queries SET surface = 'search'")
    op.alter_column("search_queries", "surface", nullable=False)

    # Nullable and staying nullable: a `search` row has no tier.
    op.add_column("search_queries", sa.Column("tier", sa.String(length=6), nullable=True))

    op.create_index("ix_search_queries_at", "search_queries", ["at"])

    # Both quoted from `m08a_curation.py`'s docstring rather than re-derived.
    #
    #   -- dashboard 5's "LLM spend per day and month" and the cost-anomaly
    #   -- alert ("daily spend > 3x the trailing 7-day median"), both
    #   -- WHERE at >= :since
    #   CREATE INDEX ix_llm_calls_at ON llm_calls (at);
    op.create_index("ix_llm_calls_at", "llm_calls", ["at"])
    #   -- dashboard 5's "cost per curated row", joining curated_rows on
    #   -- generation_id. PARTIAL: query-expansion rows carry NULL and, once
    #   -- Task 20 ships, are the majority of the table -- they are exactly
    #   -- the rows this join never wants.
    #   CREATE INDEX ix_llm_calls_generation_id ON llm_calls (generation_id)
    #       WHERE generation_id IS NOT NULL;
    #
    # Hand-written, and the predicate is why: `--autogenerate` is blind to a
    # partial index's predicate in the direction that matters, so a generated
    # spelling of this line would be a *full* index answering every membership
    # check a partial one does.
    op.create_index(
        "ix_llm_calls_generation_id",
        "llm_calls",
        ["generation_id"],
        postgresql_where=sa.text("generation_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Load-bearing, all three -- see the module docstring. Nothing else drops
    # them, and both tables outlive this revision.
    op.drop_index("ix_llm_calls_generation_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_at", table_name="llm_calls")
    op.drop_index("ix_search_queries_at", table_name="search_queries")

    op.drop_column("search_queries", "tier")
    op.drop_column("search_queries", "surface")
