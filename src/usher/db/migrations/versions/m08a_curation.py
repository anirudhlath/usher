"""`curated_rows` and `llm_calls` — what a generation produced, and its cost.

Revision ID: m08a
Revises: ffc
Create Date: 2026-08-05

**This migration opens a new revision-id convention, and that is the first
thing to record because the old one is exhausted.** `fa2b6c1e9d30`'s scheme
fixed one hex character per migration; M6's cycle ended at `fc`, M7 spent
`fd`/`fe`/`ff` and then extended by a character for `ffa`/`ffb`/`ffc`, and no
hex character sorts after `f`. Extending again (`ffca`, `ffcb`, …) keeps
sorting correctly and stops saying anything — the ids no longer group, and
nothing in them says which milestone shipped what. So **M8 opens `m08a`,
`m08b`, …**: milestone-prefixed, obviously ordered, unbounded, and still `ls`-
sortable within a milestone, which is the only thing the hex convention ever
bought. `m` sorts after `f`, so **every M8 revision sorts after every M7 one**
— verified by listing the versions directory, which is also where you can see
that a third hex cycle starting with a digit would have sorted *before* `fa`
and lost that property outright.

**The milestone number is zero-padded to two digits, and that is the whole of
why this is `m08a` and not `m8a`.** Unpadded, `sorted(["m8a", "m9a", "m10a"])`
is `["m10a", "m11a", "m8a", "m9a"]` — `m10a` sorts *first*, because `"1" <
"8"` and string comparison never reaches the `0`. That is precisely the
failure this docstring cites one paragraph up as the reason not to start a
third hex cycle, and it would have arrived one milestone after the convention
that was sold on avoiding it. M10 is not hypothetical here: it is named five
times below as the milestone that reads `llm_calls`. Two digits carries to
M99.

**The general shape, because this is the second instance of it in this
milestone:** an identifier minted by *counting* and then compared as a
*string* sorts wrong at the first two-digit value. Task 7 hit the identical
bug in `curated_rows.slug` — `curated-1 < curated-10 < curated-2` under
`HomeService`'s `(-score, slug)` tie-break. Same root cause, two subsystems,
one milestone, and both were sold on the ordering being obvious. Recorded in
`.claude/rules/db-and-sql.md` in this same commit, beside the entry saying the
old convention ran out.

**Closed in Task 13, not Task 15 as this paragraph said it was filed for.**
`services.curation_validate` is the only thing that mints a curated slug, and
it zero-pads to the width of the generation — three rows stay `curated-1` …
`curated-3`, ten become `curated-01` … `curated-10`. The fix went where the
value is minted rather than where it is read, which is the first of the two
repairs the general shape above admits; this migration's own `m08a` is the
second (pad the identifier), and the reason the slug took the same one is that
a reader ordering on `position` still exists and should not have to know.

**Two tables that share no column, no foreign key and no lifetime**, in one
migration because they are written by one service in one transaction and
because a cost ledger separated from the thing it paid for is a dashboard
reporting spend nobody can attribute to an outcome (PRD 10's dashboard 5).

---

## `curated_rows.card_title_ids` is a `uuid[]`, not a child table

The full argument, the three consequences of having no foreign key over an
array, and the reason the child table would not have bought integrity are in
`db/models/curation.py`'s module docstring. The short form, because a
migration is what an operator reads:

- **The order is the product.** A curated row *is* an ordering; it is the
  only judgement the completion was bought for. An array stores that order;
  a child table stores a `rank` every read has to remember to sort by, and a
  UUIDv7 primary key makes a forgotten `ORDER BY rank` pass every test whose
  fixture inserted the cards in order. This repository has paid for that five
  times.
- **Postgres has no foreign key over array elements**, so deleting a title
  leaves a dangling id here. The stored row still validates; the *hydration*
  loses a card; and the next nightly generation replaces the row wholesale,
  so the window is one day. The child table's alternatives are worse in kind:
  `ON DELETE CASCADE` can empty a curated row inside the database, which is
  the heading-with-no-shelf that `min_length=1` exists to refuse, and
  `ON DELETE RESTRICT` makes a title undeletable because a model mentioned it
  last night.
- **`ck_curated_rows_cards_have_no_nulls` is what a child table's `NOT NULL`
  would have been.** A `uuid[]` admits a NULL element and a child column does
  not. `array_position` is `IMMUTABLE` on PostgreSQL 17
  (`pg_proc.provolatile = 'i'`, read directly) and does find a NULL element
  (`array_position(ARRAY[<uuid>, NULL], NULL)` → `2`, run directly), so the
  CHECK is both legal and effective.

## `llm_calls.cost_usd` is `NUMERIC(12, 8)`, and both numbers are measured

`Decimal` has no prior art in this schema — every float-ish column so far is
`Float` — so the precision and scale are an unprecedented choice. Postgres
**rounds** to the declared scale on insert rather than refusing, which makes a
scale that is too small silent.

Measured on `pgvector/pgvector:pg17`, on this host, against the exact values
`OpenAICompatibleClient._cost` produces
(`(tokens_in x price_in + tokens_out x price_out) / 1_000_000`, all
`Decimal`). **Both columns of the table are filled, including the ones that
flatter nothing:**

    what                                      numeric(12,8)  numeric(12,6)  numeric(12,4)
    ----------------------------------------  -------------  -------------  -------------
    $3/Mtok x 1,200 in                           0.00360000       0.003600         0.0036
    + $15/Mtok x 340 out (PRD 10's example)      0.00870000       0.008700         0.0087
    $0.02/Mtok x 200 tok                         0.00000400       0.000004      >> 0.0000
    $0.02/Mtok x 1 tok                           0.00000002    >> 0.000000      >> 0.0000
    $0.0375/Mtok x 101 tok                       0.00000379    >> 0.000004      >> 0.0000
    $15/Mtok x 128,000 tok                       1.92000000       1.920000         1.9200

The `>>` cells are the failure: a ledger that stores a real call as `0.0000`
reports a hosted model as free, and it does so for the *cheapest* calls while
the expensive ones look right — so the monthly total is wrong by an amount
nobody can see. Same failure class as this repository's `1 / (60 + rank)`
integer division.

**Scale 8 = 2 + 6, and that is where it comes from**: a published
per-million-token price carries two decimal places (`$0.15`, `$3.00`,
`$15.00`), an integer token count adds none, and dividing by 1e6 shifts six.
At that shape the stored value is **exact**, which is the entire reason the
domain model is `Decimal` — verified, `0.0036` reads back equal to `0.0036`.

**What scale 8 still costs, since it is not free.** A price with more than two
decimal places can produce a ninth: `$0.0375/Mtok x 101 tokens` is exactly
`0.0000037875` and stores as `0.00000379`. The residual is bounded by 5e-9 USD
per call — one dollar after two hundred million calls. Scale 10 removes it and
claims a resolution the *input* does not have: both prices are an operator's
guess at a provider's sheet and both default to `0`.

**Precision 12 → four integer digits, so a call above `$9,999.99999999`
raises `numeric field overflow` rather than storing something.** Verified. It
is a ceiling on the misconfiguration that scales a price **up** by a million:
`_cost` already divides by `1_000_000`, so an operator entering `3_000_000`
where `3` was meant is charged 1e6 times the real number, and at 12,000 tokens
that is `$36,000`.

**`db/models/curation.py`'s module docstring holds the one copy of that
argument**, including the two limitations it does not cover — the same error
on a smaller call fits and stores, and the *inverse* error (a per-token price
in a per-Mtok field) under-states by the same factor with no ceiling at all.
This paragraph shipped with the direction reversed, said "a price entered per
*token* instead of per million", and that mistake produces the opposite of
`$36,000`; it was wrong in all five copies, so there is now one. A month of
three-figure spend is unaffected either way; that is a `SUM()`, computed at
unconstrained precision, so this bound is per call and not per ledger.

## Indexes, each with the query it serves and the alternative refused

**`ix_curated_rows_user_newest (user_id, generated_at DESC)` — three
readers.** `CuratedRowRepository.list_for_user`'s `WHERE user_id = :user_id`;
`replace_for_user`'s `DELETE` by the same column; and `users`' `ON DELETE
CASCADE`, which Postgres performs as a lookup *by the referencing column* —
`ix_media_items_episode_id`'s argument verbatim.

**The `DESC` is not observable in a plan, and that is measured rather than
assumed.** On `pgvector/pgvector:pg17` at 30,000 rows (2,000 users x 3
generations x 5 rows, far above any real household), same session, same
`ANALYZE`:

    index declared                     ORDER BY               plan
    ---------------------------------  ---------------------  -----------------------
    (user_id, generated_at)            generated_at DESC      Index Scan Backward
    (user_id, generated_at DESC)       generated_at DESC      Index Scan
    (user_id, generated_at)            ... DESC, position     Bitmap Heap Scan + Sort
    (user_id, generated_at DESC)       ... DESC, position     Bitmap Heap Scan + Sort
    (user_id, generated_at DESC, pos)  ... DESC, position     Bitmap Heap Scan + Sort

    (every query is `WHERE user_id = ?`; the first pair carries `LIMIT 1`)

A btree is bidirectional and the leading column is fixed by equality, so the
descending scan is free either way; and at five rows a user the planner
prefers a sort to an ordered scan no matter how many keys the index has. So
the direction is declared for what it costs — nothing — and for what a *wrong*
one would cost later. `ffc`, one revision down, dropped `ix_titles_popularity`
precisely because its declared direction did not match any statement's
pathkeys; the day this read grows a second ordering key, or a retention policy
makes the per-user set large enough for an ordered scan to win, a `DESC` that
was already right is the difference between an index scan and a full sort.
`test_the_row_read_indexes_carry_the_clauses_that_make_them_work` reads it
back off `pg_indexes.indexdef`, because `compare_metadata` is blind to a
partial predicate and this project does not want to depend on which clauses a
future Alembic renders.

**Also honest: at household scale the planner will seq-scan this table
anyway.** `curated_rows` holds one generation per user — tens of rows — and
the index's value today is the referential lookup plus a floor under the day
the table is not tiny. That is `ix_media_items_episode_id`'s position exactly,
which is why `test_both_new_foreign_keys_have_an_index_the_referential_check_
can_use` forces the plan with `enable_seqscan = off`: an empty table
seq-scans regardless of how many indexes it has, and proves nothing.

**No `UNIQUE (user_id, slug)`.** It would hold today, because
`replace_for_user` is delete-then-insert in one transaction and leaves one
generation per user — which is what makes it the wrong constraint: it encodes
the *writer's transaction shape* into the schema. Under it a second generation
landing before the first is cleared is a failed write; without it, it is a
stale screen the `generated_at DESC` read steps over. A legibly stale shelf
beats a generation that cannot be stored. It would also foreclose retaining
the last N generations, which is what dashboard 5 wants the day "cost per
curated row" is asked over a window rather than about tonight.

**`llm_calls` ships with its primary key and nothing else, deliberately** —
`genome_scores`' precedent, and `ffc`'s argument. Every reader of this table
named anywhere in the PRD is a Grafana panel that **M10** builds, and Task
10's `LLMCallRepository` is append-only with no read method at all, so after
M8 this table has **zero** readers in `src/`. An index nothing reads is
`ix_titles_popularity` again: maintained on every write, for a consumer that
does not exist. Written down so this is a deferral rather than a deletion —
**the two that will be right, and the query each serves:**

    -- dashboard 5's "LLM spend per day and month" and the cost-anomaly alert
    -- ("daily spend > 3x the trailing 7-day median"), both WHERE at >= :since
    CREATE INDEX ix_llm_calls_at ON llm_calls (at);

    -- dashboard 5's "cost per curated row", joining curated_rows on
    -- generation_id. PARTIAL: query-expansion rows carry NULL and, once Task
    -- 20 ships, are the majority of the table -- they are exactly the rows
    -- this join never wants.
    CREATE INDEX ix_llm_calls_generation_id ON llm_calls (generation_id)
        WHERE generation_id IS NOT NULL;

Neither is worth adding before the statement that reads it, and both should
arrive with a measurement against a real ledger rather than against this
paragraph. **Not indexed even then: `purpose` and `model`** — a deployment
holds one or two values of each, so a btree over either is a structure with
two entries, which is `title_embeddings.model_name`'s refusal one module over.

## Timestamps, triggers, and what this migration deliberately does not add

**No `created_at`, no `updated_at`, and no trigger on either table.** Both are
write-once artefacts: a curated row is replaced wholesale and an `llm_calls`
row records something that already happened, so `generated_at` and `at` are
the only timestamps that mean anything. `title_neighbors` and `genome_scores`
are the precedent, and it is mechanically required as well —
`test_migration_creates_the_updated_at_triggers` asserts the trigger set
**exactly** at seven names, so a trigger here is a failing case in another
file.

**And neither timestamp gets a `server_default`, which is the one place this
migration departs from every other timestamp in the schema.** `generated_at`
is one instant per *generation*, written identically onto every row of it —
which is what makes `ORDER BY generated_at DESC` select a whole generation
rather than a mixture. A `server_default=now()` would hand each row its own
value the moment a writer omitted the column, and the rows of one shelf set
would sort apart. `at` is the same argument: it is when the completion
happened, not when the row was inserted.

**`llm_calls` has no foreign key at all.** No `user_id`, deliberately and as
PRD 10 specifies — spend is attributed to an outcome by joining `curated_rows`
on `generation_id`. And `generation_id` references nothing: a foreign key to
`curated_rows.generation_id` would require that column to be unique, which it
is not (one generation is three to five rows) and must not become; a foreign
key to a `generations` table would require inventing that table, whose columns
would be this one; and *any* foreign key makes a ledger row deletable by a
cascade from the thing whose cost it records, which is backwards — a curated
row is replaced nightly and the money was still spent.

Reversible in both directions. `downgrade()` drops exactly the two tables this
creates and nothing else, and their indexes and constraints go with them.
Verified empty → head → `downgrade base` → head against a real
`pgvector/pgvector:pg17`.
"""

import sqlalchemy as sa
from alembic import op

from usher.db.models.curation import COST_PRECISION, COST_SCALE

revision = "m08a"
down_revision = "ffc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "curated_rows",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # CASCADE, and it is `user_taste`'s case rather than `watch_states`'.
        # ADR-0010 makes `watch_states.user_id` RESTRICT because a watch
        # record is state a delete would destroy irrecoverably; a curated row
        # protects nothing and is re-derived by running the generation again.
        # RESTRICT here would make deleting a user fail because a model wrote
        # them a shelf last night.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_curated_rows_user_id_users"),
            nullable=False,
        ),
        # `curated-1`, `curated-2`, … minted from the row's position rather
        # than slugified from the model's title: `RowCache` keys on
        # `(user_id, slug)`, so two generations producing the same title would
        # collide, and the composer breaks score ties on `slug`, so a
        # positional slug makes the model's own ordering the tiebreak instead
        # of an alphabetisation of its prose.
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        # Nullable, and reachable: none of M7's nine providers can produce a
        # row with nothing to explain. A model that returns an empty reason
        # should give a row with no subtitle rather than one with an empty one.
        sa.Column("reason", sa.Text(), nullable=True),
        # `uuid[]`, ordered, and the order is the product. No foreign key is
        # possible over array elements -- see this docstring's first section
        # for the three consequences and why the child table is worse.
        sa.Column(
            "card_title_ids",
            sa.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        # The model's own ordering of rows within one generation. `position`
        # is a Postgres keyword; SQLAlchemy quotes the identifier and the
        # CHECK below quotes it by hand.
        sa.Column("position", sa.Integer(), nullable=False),
        # ADR-0020's shape, applied to a generation: it makes "these rows were
        # written by a model we no longer run" a query rather than something
        # inferred from a date. Deliberately not an invalidation predicate --
        # nothing recomputes curated rows on a model change, because
        # regeneration is an operator's job either way.
        sa.Column("model_name", sa.Text(), nullable=False),
        # What makes a replacement atomic and a partial write visible, and
        # what dashboard 5 joins `llm_calls` on.
        sa.Column("generation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # No `server_default` -- one instant per generation, written
        # identically onto every row of it. See this docstring.
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_curated_rows"),
        # `CuratedRow`'s Pydantic bounds, mirrored -- this schema's standing
        # convention, because nothing stops a hand-written `INSERT` from
        # bypassing the model.
        sa.CheckConstraint("slug <> ''", name="ck_curated_rows_slug_not_empty"),
        sa.CheckConstraint("title <> ''", name="ck_curated_rows_title_not_empty"),
        sa.CheckConstraint("model_name <> ''", name="ck_curated_rows_model_name_not_empty"),
        sa.CheckConstraint('"position" >= 0', name="ck_curated_rows_position_non_negative"),
        # `card_title_ids`' `min_length=1`, in SQL. An empty curated row is
        # not a state -- it is a validator that ran and kept nothing -- and
        # storing one puts a heading with no shelf under it on the screen.
        sa.CheckConstraint(
            "cardinality(card_title_ids) > 0", name="ck_curated_rows_cards_not_empty"
        ),
        # What a child table's `NOT NULL` would have been.
        sa.CheckConstraint(
            "array_position(card_title_ids, NULL) IS NULL",
            name="ck_curated_rows_cards_have_no_nulls",
        ),
    )
    # The read, the delete, and the cascade's own lookup -- one index for
    # three. `DESC` is not plan-observable at this population (measured, see
    # the docstring) and is declared because a wrong direction is what `ffc`
    # dropped an index for.
    op.create_index(
        "ix_curated_rows_user_newest",
        "curated_rows",
        ["user_id", sa.text("generated_at DESC")],
        unique=False,
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # When the completion happened, not when the row was inserted, so no
        # `server_default`. `at` rather than `created_at` because PRD 10's
        # column list says `at` and the two would be the same instant.
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        # Recorded per call rather than read from configuration, so a row
        # survives an operator changing `USHER_LLM_MODEL`.
        sa.Column("model", sa.Text(), nullable=False),
        # `VARCHAR(32)` via `native_enum=False`, this schema's only enum
        # spelling -- no `CREATE TYPE`, no membership CHECK, Pydantic owns
        # membership. `query_expansion` is the longest member at 15
        # characters. PRD 10 *wrote* this column's vocabulary open-ended
        # ("curation | query_expansion | …"); this commit closes it in both
        # places -- the column here, and the trailing "| …" struck from PRD 10
        # -- and a new call site now adds a member to `LLMPurpose` and to
        # PRD 10 in one change.
        sa.Column(
            "purpose",
            sa.Enum("curation", "query_expansion", name="llmpurpose", native_enum=False, length=32),
            nullable=False,
        ),
        # Recorded exactly, which is the mitigation for both prices defaulting
        # to 0: spend is recomputable from this ledger after the fact when an
        # operator discovers they never priced a hosted model.
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        # `NUMERIC(12, 8)`, never `Float`. The measured table is in this
        # docstring; the constants are imported so the model and this
        # migration cannot drift.
        sa.Column("cost_usd", sa.Numeric(COST_PRECISION, COST_SCALE), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        # Not "the HTTP call returned 200" -- it is "this generation produced
        # something", and a call that answered perfectly and validated to zero
        # rows is `ok = false` with a reason (ADR-0028).
        sa.Column("ok", sa.Boolean(), nullable=False),
        # Present exactly when `ok` is false, enforced below as well as by
        # `LLMCall._ok_and_error_must_agree`.
        sa.Column("error", sa.Text(), nullable=True),
        # Nullable: a purpose that produces no rows at all has no generation.
        # Query expansion is one, and once Task 20 ships these are the
        # majority of the table. No foreign key -- see this docstring.
        sa.Column("generation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        sa.CheckConstraint("model <> ''", name="ck_llm_calls_model_not_empty"),
        sa.CheckConstraint("tokens_in >= 0", name="ck_llm_calls_tokens_in_non_negative"),
        sa.CheckConstraint("tokens_out >= 0", name="ck_llm_calls_tokens_out_non_negative"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_calls_cost_usd_non_negative"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_llm_calls_latency_ms_non_negative"),
        # `LLMCall._ok_and_error_must_agree`'s two clauses. Its docstring puts
        # the enforcement on the model "rather than as a CHECK alone" -- alone
        # being the operative word, and the more so because it is a
        # `model_validator(mode="after")`, which `model_construct` skips
        # entirely. The ledger outlives the process that wrote it, and a row
        # where `ok` is true and `error` is set reads as a failure in every
        # `WHERE error IS NOT NULL` anybody will write.
        sa.CheckConstraint(
            "(ok AND error IS NULL) OR (NOT ok AND error IS NOT NULL AND error <> '')",
            name="ck_llm_calls_ok_error_agree",
        ),
    )
    # No index on `llm_calls` beyond its primary key. The two that will be
    # right, and the query each serves, are in this migration's docstring.


def downgrade() -> None:
    op.drop_table("llm_calls")
    # **Not load-bearing, and kept anyway so `downgrade()` mirrors `upgrade()`
    # statement for statement and a reader can diff the two by eye.**
    # `op.drop_table` on the next line takes the index with it regardless, so
    # deleting this line is an equivalent mutation -- unlike `ff`'s downgrade,
    # where the `create_index` is the only thing that restores the index and
    # removing it really does leave the schema short. Said explicitly because
    # the two lines look alike and a future author would otherwise read this
    # one as necessary.
    op.drop_index("ix_curated_rows_user_newest", table_name="curated_rows")
    op.drop_table("curated_rows")
