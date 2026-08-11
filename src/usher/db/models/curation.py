"""`curated_rows` and `llm_calls` — what a generation produced, and what it
cost.

Two tables in one module for `domain/curation.py`'s reason: they are written
by one service in one transaction, and reading either without the other is
how a cost dashboard ends up reporting spend nobody can attribute to an
outcome. They share no column, no foreign key and no lifetime.

**The class names are `CuratedRowRow` and `LLMCallRow`.** The first is ugly
and is the naming convention (`<Thing>Row`) meeting a domain type whose name
already ends in `Row`, because a curated row *is* a row on a screen. Renaming
either side to avoid the stutter would make the model's name lie about what
PRD 06 calls it, and the alternative spelling (`CuratedRowsRow`) trades a
stutter for a plural. Recorded so nobody assumes it was a typo.

## `card_title_ids` is a `uuid[]` on the row, not a child table

This is the one design question these two tables had, and both shapes are
precedented here: `titles.genres` is `ARRAY(Text)`, and `title_neighbors` is
the child table with an explicit `rank` integer. The array wins on three
counts and loses on one, and the one is stated below rather than left to be
discovered.

**The ordering is the product.** `CuratedRow`'s docstring: a curated row *is*
an ordering, it is the only judgement the completion was bought for, and
nothing downstream may re-sort it. In an array the order is the storage —
Postgres arrays are ordered containers, so there is no `ORDER BY` for a
reader to forget. In a child table the order is a `rank` column that every
single read has to remember to sort by, and this repository has already paid
for that mistake five times over: a UUIDv7 primary key makes `ORDER BY id`
and `ORDER BY <the real key>` agree by accident, so a missing `ORDER BY rank`
is green in every test whose fixture inserted the cards in order.
`title_neighbors` carries its own warning about this and its ordering is a
*ranking*, which a client may legitimately re-derive; this one is not.

**A shelf is one row, so a replacement is one statement per shelf.**
`replace_for_user` is delete-then-insert in one transaction. In the array
shape that is `DELETE … WHERE user_id = :u` plus an insert of three to five
rows. In the child shape it is the same plus thirty to fifty child rows, an
extra `ON DELETE CASCADE` to lean on, and — the part that matters — a
partially inserted shelf becomes representable, which is exactly the state
`CuratedRow`'s `min_length=1` exists to make unconstructible.

**The 1:1 row/model rule stays spellable.** `CuratedRow` has ten fields and
this table has ten columns. A child table leaves nine here and puts the tenth
somewhere `SELECT *` cannot see, so `PostgresCuratedRowRepository` stops
being able to read through the house shape (a `SELECT *` into an
`extra="forbid"` model) and needs an aggregate, and `test_db_models_curation.
py`'s assertion needs an exception list. `titles` is the only table in this
schema with such a list and it exists for generated columns.

**What it costs, and it is not a footnote: this column cannot have
referential integrity.** PostgreSQL has no foreign key over array elements —
there is no `FOREIGN KEY EACH ELEMENT` — so a `title_id` inside
`card_title_ids` is an ordinary 16-byte value that Postgres will never check
and never cascade. Deleting a title therefore leaves a dangling id in every
curated row that mentioned it. Three consequences, in the order they arrive:

- **The stored row still validates.** The ids are all still there, so the
  read into `CuratedRow` is unaffected; the domain model never claims the ids
  resolve.
- **`LLMRow.build`'s hydration shortens the shelf.** A card whose title is
  gone is a lookup that returns nothing, so the shelf loses a card and the
  heading stays. That is [ADR-0014](../../../../docs/prd/decisions/0014-absence-is-not-zero.md)'s
  shape and it is the same degradation the validator already produces. The
  cases are `tests/unit/test_rows_curated.py`'s, and the vanished id sits in
  the *middle* of the array there, which is what rules out a hydration that
  stops at the gap; a shelf that empties entirely builds empty and is dropped
  by the composer rather than rendered as a heading with nothing under it.
- **It self-heals at the next generation**, because `curated_rows` holds one
  generation per user and the next nightly run replaces it wholesale. The
  window is one day.

**And the child table would not actually have bought integrity — it would
have bought a choice between two worse outcomes.** `ON DELETE CASCADE` on the
card's `title_id` deletes the card, which can empty a curated row and produce
precisely the heading-with-no-shelf that `min_length=1` refuses, silently and
in the database rather than in the hydration where it is visible.
`ON DELETE RESTRICT` refuses to delete any title an LLM happened to mention
last night, which for a fully re-derivable artefact is a delete that can
essentially never succeed — `title_neighbors` refuses RESTRICT for that exact
reason. So the missing foreign key costs a dangling id for at most one
generation, and the foreign key would have cost either a malformed row or an
undeletable catalog.

**One liability the array really does introduce is closed here:** a `uuid[]`
admits a NULL *element*, which a `NOT NULL` child column could not, and a
NULL element reads back as a card that denotes nothing.
`ck_curated_rows_cards_have_no_nulls` is what a child table's `NOT NULL`
would have been. `array_position` is `IMMUTABLE` (verified on PostgreSQL 17,
`pg_proc.provolatile = 'i'`) and finds a NULL element (verified —
`array_position(ARRAY[<uuid>, NULL], NULL)` returns 2), so the CHECK is both
legal and effective.

**Note the related trap this shape avoids by having exactly one array.**
`unnest(uuid[], text[][])` flattens a two-dimensional array, so any
parallel-array spelling — ids in one column and reasons in another — silently
mispairs. There is one array here and nothing is parallel to it.

## `cost_usd` is `NUMERIC(12, 8)`, and the numbers are measured

`Decimal` has no prior art in this schema; every float-ish column so far is
`Float`. `LLMCall.cost_usd` is the first, and it is `Decimal` because
`$3/Mtok x 1,200 tokens` is exactly `0.0036`, a value binary floating point
cannot represent, and because this number is summed over a month. A `NUMERIC`
column with the wrong scale throws that away at the last step: Postgres
*rounds* to the declared scale on insert rather than refusing, so a scale
that is too small is silent.

**Scale 8, because a price sheet is quoted in cents per million tokens.**
`OpenAICompatibleClient._cost` computes
`(tokens_in x price_in + tokens_out x price_out) / 1_000_000` in `Decimal`,
so the result has at most `decimals(price) + 6` decimal places. At two
decimal places on the price — `$0.15`, `$3.00`, `$15.00`, `$2.50`, which is
how published per-million-token prices are written — eight is exactly enough
and the stored value is *exact* rather than rounded.

Measured on `pgvector/pgvector:pg17` against the values this column actually
receives:

| what | `numeric(12,8)` | `numeric(12,6)` | `numeric(12,4)` |
|---|---|---|---|
| `$3/Mtok x 1,200 in` | `0.00360000` | `0.003600` | `0.0036` |
| PRD 10's example (`+ $15/Mtok x 340 out`) | `0.00870000` | `0.008700` | `0.0087` |
| `$0.02/Mtok x 200 tok` | `0.00000400` | `0.000004` | **`0.0000`** |
| `$0.02/Mtok x 1 tok` | `0.00000002` | **`0.000000`** | **`0.0000`** |
| `$0.0375/Mtok x 101 tok` | `0.00000379` | `0.000004` | **`0.0000`** |
| `$15/Mtok x 128,000 tok` | `1.92000000` | `1.920000` | `1.9200` |

**The bolded cells are the failure.** A ledger that stores a real call as
`0.0000` reports a hosted model as free, and it does it for the cheapest
calls — the common case on a small model — while the expensive ones look
right, so the total is wrong by an amount nobody can see. That is the same
failure class as this repository's `1 / (60 + rank)` integer division.

**What scale 8 still costs, stated rather than implied.** A price carrying
more than two decimal places can produce a ninth: `$0.0375/Mtok x 101 tokens`
is exactly `0.0000037875` and stores as `0.00000379`. The residual is bounded
by 5e-9 USD per call — one dollar after two hundred million calls — and it is
a real rounding rather than an exact value. Scale 10 would remove it and buy
nothing an operator can act on, while claiming a resolution the *input* does
not have: both prices are operator-configured guesses at a provider's sheet
and both default to `0`.

**Precision 12, so four integer digits, so a single call above
`$9,999.99999999` raises `numeric field overflow` rather than storing
something.** Verified.

**This paragraph is the one copy of what that ceiling catches**, and the
consolidation is a correction rather than tidying: the mechanism shipped in
five places and every copy had the direction backwards, saying "a price
entered per *token* instead of per million". That mistake produces the
opposite of `$36,000`. Four other places — `m08a`'s docstring,
`LLMCallRepository.record`, `PostgresLLMCallRepository` and
`tests/fakes/llm_call_repository.py` — now name the ceiling and point here.

The misconfiguration is a price **scaled up** by a million on the way in.
`OpenAICompatibleClient._cost` already divides by `1_000_000`, so an operator
who performs the per-million conversion themselves — entering `3_000_000`
where `3` was meant — is charged 1e6 times the real number, and at 12,000
tokens that is `$36,000` and fails loudly. `Settings` bounds both price fields
below and not above, so such a value is accepted (verified), which is what
makes this reachable rather than theoretical.

**Two honest limitations, and the second is the larger one.** The same
over-statement on a 1,200-token call is `$3,600`, which fits and stores, so
the ceiling catches the large version and not the small one. And the
**inverse** mistake has no ceiling at all: entering the *per-token* price
(`$0.000003` for a `$3`/Mtok model) into a per-Mtok field under-states every
call by that same factor of 1e6, stores perfectly, and reads back as a hosted
model that is nearly free. That is the `0.0000` failure of the table above
arriving through the *input* rather than through the scale, and nothing in
this schema can see it — which is also why `cost_usd` recording the token
counts alongside it matters: spend is recomputable from this ledger after the
fact, and that is the only repair either direction has.

A month of three-figure spend is unaffected by the ceiling either way: the
report is a `SUM()`, which Postgres computes at unconstrained precision, so
this bound is per call and not per ledger.

## Neither table has an `updated_at`, and neither has a trigger

Both are write-once artefacts. A curated row is replaced wholesale, never
updated; an `llm_calls` row records something that already happened. This
follows `title_neighbors` and `genome_scores`, and it is mechanically
required as well: `tests/integration/test_migrations.py::
test_migration_creates_the_updated_at_triggers` asserts the trigger set
**exactly**, so a trigger here is a failing case in another file.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.curation import LLMPurpose

#: `NUMERIC(12, 8)`. Declared as a constant because the migration and this
#: model must agree exactly or `test_migration_matches_the_orm_metadata`
#: reports drift, and because the two numbers are a decision rather than a
#: default -- see the module docstring's measured table.
COST_PRECISION = 12
COST_SCALE = 8


class CuratedRowRow(Base):
    """One shelf an LLM proposed, after validation, as stored.

    Ten columns for `CuratedRow`'s ten fields, with **no `created_at`**:
    `generated_at` is the generation's own instant and a second timestamp
    would differ from it by the width of a transaction.

    **The table holds one generation per user.** `replace_for_user` is
    delete-then-insert in one transaction, so a committed state never
    contains two. That is a property of the writer rather than of the schema,
    and the read is deliberately written to survive its violation — see
    `ix_curated_rows_user_newest` below, and the unique constraint refused
    beside it.
    """

    __tablename__ = "curated_rows"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE, and it is `user_taste`'s case rather than `watch_states`'.
    # ADR-0010 makes `watch_states.user_id` protect state a delete would
    # destroy irrecoverably; a curated row protects nothing and is re-derived
    # by running the generation again. RESTRICT would make deleting a user
    # fail because a model wrote them a shelf last night.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # `curated-1`, `curated-2`, … zero-padded to the width of the generation,
    # so ten rows are `curated-01` … `curated-10`. `RowCache` keys on
    # `(user_id, slug)` and the composer breaks score ties on `slug`, so this
    # is minted from the row's position rather than slugified from the model's
    # title -- see `CuratedRow.slug`'s comment for the three reasons and for
    # why the padding is not cosmetic.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    # The model's own prose, rendered as the shelf heading -- the one string
    # in this schema that a language model wrote and a user reads verbatim,
    # which is why ADR-0028's validator is the only thing between the two.
    # `Text` rather than `String(N)`, following `source_fingerprint`: in
    # Postgres a `varchar(N)` overflow is an *error*, so a length cap here
    # would fail a whole generation over one long heading, and how much of a
    # heading fits is a client layout concern rather than a fact about the
    # shelf.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable, and reachable: none of M7's nine providers can produce a row
    # with nothing to explain, and a model that returns an empty reason
    # should give a row with no subtitle rather than a row with an empty one.
    reason: Mapped[str | None] = mapped_column(Text)
    # `uuid[]`, ordered, and the order is the product. The module docstring
    # carries the argument against the child table and the three consequences
    # of having no foreign key here.
    #
    # `Mapped[list[...]]` rather than the domain's `tuple[...]`: `ARRAY`
    # accepts a tuple on write and always returns a list on read -- the same
    # asymmetry `titles.genres` records.
    card_title_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    # The model's own ordering of the rows within one generation, `ge=0`
    # because it indexes the list the model returned. Quoted in the CHECK
    # below because `position` is a Postgres keyword; SQLAlchemy quotes the
    # identifier itself.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # `title_embeddings.model_name`'s reason (ADR-0020): it makes "these rows
    # were written by a model we no longer run" a query rather than something
    # inferred from a date. Deliberately *not* an invalidation predicate --
    # nothing recomputes curated rows on a model change.
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    # What makes a replacement atomic and a partial write visible. No foreign
    # key: there is no `generations` table and inventing one would be a row
    # per generation carrying nothing this column does not already carry.
    generation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # **No `server_default`, unlike every other timestamp in this schema.**
    # This is one instant per *generation*, written identically onto every
    # row of it, which is what makes `ORDER BY generated_at DESC` select a
    # whole generation instead of a mixture. A server default would give each
    # row of one shelf set its own value the moment a writer omitted the
    # column, and the rows of one generation would sort apart.
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # **The read.** `list_for_user` is `WHERE user_id = :user_id`
        # ordered by the newest `generated_at`; `replace_for_user`'s `DELETE`
        # is the same lookup; and Postgres implements `users`' `ON DELETE
        # CASCADE` by finding referencing rows *by this column*, which is
        # `ix_media_items_episode_id`'s argument verbatim. Three readers, one
        # index.
        #
        # **`generated_at DESC` is the direction the read asks for and it is
        # not observable in a plan -- measured, and recorded rather than
        # claimed.** On `pgvector/pgvector:pg17` at 30,000 rows (2,000 users
        # x 3 generations x 5 rows, far above any real household), `WHERE
        # user_id = :u ORDER BY generated_at DESC LIMIT 1` plans as `Index
        # Scan Backward` against an ascending index and `Index Scan` against
        # this one -- same shape, same cost, because a btree is
        # bidirectional and the leading column is fixed by equality. Adding
        # `position` as a third key does not change that either: at five rows
        # a user the planner picks `Bitmap Heap Scan` + `Sort` over an
        # ordered scan regardless of how the index is declared.
        #
        # So the direction is declared for what it costs (nothing) and for
        # what a wrong one would cost later: `ffc` dropped
        # `ix_titles_popularity` because its declared direction did not match
        # any statement's pathkeys, and the day this read grows a second
        # ordering key -- or a retention policy makes the per-user set big
        # enough for an ordered scan to win -- a `DESC` that was already
        # right is the difference between an index scan and a full sort.
        # `compare_metadata` does diff this clause (measured in M7, in both
        # directions) and `test_the_row_read_indexes_carry_the_clauses_that_
        # make_them_work` reads it back off `pg_indexes.indexdef`.
        #
        # **No `UNIQUE (user_id, slug)`, and the refusal is the read's own
        # argument.** It would hold today, because `replace_for_user` leaves
        # one generation per user -- which is what makes it the wrong
        # constraint: it encodes the *writer's transaction shape* into the
        # schema. Under it, a second generation landing before the first was
        # cleared is a failed write; without it, it is a stale screen the
        # `generated_at DESC` read steps over. A legibly stale shelf beats a
        # generation that cannot be stored. It would also foreclose keeping
        # the last N generations, which is what PRD 10's dashboard 5 would
        # want the day "cost per curated row" is asked over a window rather
        # than about tonight.
        Index(
            "ix_curated_rows_user_newest",
            "user_id",
            text("generated_at DESC"),
        ),
        # `min_length=1` on the three text fields, mirrored -- this schema's
        # standing convention, because nothing stops a hand-written `INSERT`
        # from bypassing the Pydantic model.
        CheckConstraint("slug <> ''", name="ck_curated_rows_slug_not_empty"),
        CheckConstraint("title <> ''", name="ck_curated_rows_title_not_empty"),
        CheckConstraint("model_name <> ''", name="ck_curated_rows_model_name_not_empty"),
        CheckConstraint('"position" >= 0', name="ck_curated_rows_position_non_negative"),
        # `CuratedRow.card_title_ids`' `min_length=1`, in SQL. An empty
        # curated row is not a state -- it is a validator that ran and kept
        # nothing -- and storing one puts a heading with no shelf under it on
        # the screen. The row is discarded whole instead, never padded from
        # the pool.
        CheckConstraint("cardinality(card_title_ids) > 0", name="ck_curated_rows_cards_not_empty"),
        # The array shape's one liability, closed. A child table's `NOT NULL`
        # would have done this; see the module docstring.
        CheckConstraint(
            "array_position(card_title_ids, NULL) IS NULL",
            name="ck_curated_rows_cards_have_no_nulls",
        ),
    )


class LLMCallRow(Base):
    """One *attempted* completion, whether or not it worked — PRD 10's cost
    ledger.

    **`record()` is called on both paths and `ok` is the discriminator**, so
    a ledger holding only the successes understates spend by exactly the
    failures, which are the rows an operator most wants to see.

    **No `user_id`, deliberately and as specified.** Spend is attributed to
    an outcome by joining `curated_rows` on `generation_id`, which is what
    PRD 10's dashboard 5 *is*, rather than by denormalising a household onto
    a cost row.

    **`generation_id` has no foreign key**, and the alternative is refused
    with its consequence rather than on taste. A foreign key to
    `curated_rows.generation_id` would need that column to be unique, which
    it is not (one generation is three to five rows) and must not become. A
    foreign key to a `generations` table would need that table, whose columns
    would be this one. And any foreign key at all makes the ledger's rows
    deletable by a cascade from the thing they record the cost of, which is
    backwards: a curated row is replaced nightly and the money was still
    spent. So this is a correlation id, and the join it serves is an outer
    one.
    """

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # When the *completion* happened, not when the row was inserted -- so no
    # `server_default`, for `curated_rows.generated_at`'s reason. `at` rather
    # than `created_at` because PRD 10's column list says `at` and because
    # the two would be the same instant.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The model string the request was sent with, recorded per call rather
    # than read from configuration, so a row survives an operator changing
    # `USHER_LLM_MODEL` -- `title_embeddings.model_name`'s argument applied
    # to a ledger.
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # A closed vocabulary, so `GROUP BY purpose` stays a usable telemetry
    # dimension instead of a cardinality footgun. `VARCHAR(32)`;
    # `query_expansion` is the longest member at 15 characters.
    purpose: Mapped[LLMPurpose] = mapped_column(enum_column(LLMPurpose, length=32), nullable=False)
    # Recorded exactly, and that is the mitigation for both prices defaulting
    # to `0`: spend is recomputable from this ledger after the fact when an
    # operator discovers they never priced a hosted model.
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False)
    # `NUMERIC(12, 8)`, never `Float`. The module docstring carries the
    # measured table behind both numbers.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(COST_PRECISION, COST_SCALE), nullable=False)
    # `time.monotonic()` across the whole request -- transport and decode
    # included, not the provider's own reported generation time. Written on
    # the failure path too, where it is the number that tells a timeout
    # (`USHER_LLM_TIMEOUT_SECONDS`, 120 s) apart from a fast refusal; those
    # are otherwise the same `ok = false` row. It is also what makes a
    # zero-token row legible rather than merely odd, which
    # `OpenAICompatibleClient._usage` names as the honest half of its
    # compromise: a real latency with no tokens is a provider that answered
    # and omitted `usage`.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Not "the HTTP call returned 200". It is "this generation produced
    # something", and the two disagree in exactly one direction: a call that
    # answered perfectly and validated to zero rows is `ok = false` with a
    # reason (ADR-0028). That is the only signal separating a validator that
    # ate the output from a model that had nothing to say.
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Present exactly when `ok` is false, enforced by the CHECK below as well
    # as by `LLMCall._ok_and_error_must_agree`. `Text` rather than a code: an
    # operator reads this, and what can go wrong here spans an upstream, a
    # parser and a validator.
    error: Mapped[str | None] = mapped_column(Text)
    # Nullable, because a purpose that produces no rows at all has no
    # generation -- query expansion is one, and `QueryExpansionService` writes
    # `NULL` here on every row it records, so on a deployment that curates and
    # is searched these are the majority of the table.
    generation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint("model <> ''", name="ck_llm_calls_model_not_empty"),
        CheckConstraint("tokens_in >= 0", name="ck_llm_calls_tokens_in_non_negative"),
        CheckConstraint("tokens_out >= 0", name="ck_llm_calls_tokens_out_non_negative"),
        CheckConstraint("cost_usd >= 0", name="ck_llm_calls_cost_usd_non_negative"),
        CheckConstraint("latency_ms >= 0", name="ck_llm_calls_latency_ms_non_negative"),
        # `LLMCall._ok_and_error_must_agree`'s two clauses, in SQL. Its
        # docstring says the model is the right place to enforce this
        # *"rather than as a CHECK alone"* -- alone being the operative word,
        # and the more so because it is a `model_validator(mode="after")`,
        # which `model_construct` skips entirely. The ledger outlives the
        # process that wrote it, and a row where `ok` is true and `error`
        # is set reads as a failure in every `WHERE error IS NOT NULL`
        # anybody will write against it.
        CheckConstraint(
            "(ok AND error IS NULL) OR (NOT ok AND error IS NOT NULL AND error <> '')",
            name="ck_llm_calls_ok_error_agree",
        ),
        # **No index beyond the primary key, deliberately** -- the
        # `genome_scores` precedent, and the argument is `ffc`'s. Every
        # reader named anywhere in the PRD is a Grafana panel that M10 builds:
        # dashboard 5's "spend per day and month" and its cost-anomaly alert
        # want `(at)`, and its "cost per curated row" join wants
        # `(generation_id) WHERE generation_id IS NOT NULL` -- partial,
        # because query-expansion rows carry NULL and are exactly the rows
        # that join never wants. Task 10's `LLMCallRepository` is append-only
        # and has no read method, so after M8 this table has **zero** readers
        # in `src/`, and an index nothing reads is `ix_titles_popularity`
        # again -- maintained on every write for a consumer that does not
        # exist. Both are one `CREATE INDEX` away and `m08a`'s docstring says
        # so, which is what makes this a deferral rather than a deletion.
        #
        # Not indexed even then: `purpose` and `model`. A deployment holds
        # one or two values of each, so a btree over either is a structure
        # with two entries -- `title_embeddings.model_name`'s refusal, one
        # module over.
    )
