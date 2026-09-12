"""`curated_rows` and `llm_calls` — what a generation produced, and its cost."""

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
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_curated_rows_user_id_users"),
            nullable=False,
        ),
        # `curated-1`, `curated-2`, … minted from the row's position rather than
        # slugified from the model's title: `RowCache` keys on `(user_id, slug)`, so two
        # generations producing the same title would collide, and the composer breaks
        # score ties on `slug`, so a positional slug makes the model's own ordering the
        # tiebreak instead of an alphabetisation of its prose.
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
        # `VARCHAR(32)` via `native_enum=False`, this schema's only enum spelling -- no
        # `CREATE TYPE`, no membership CHECK, Pydantic owns membership.
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
        # Query expansion is one, and `QueryExpansionService` writes one row
        # per search that embeds, so on a deployment that curates and is
        # searched these are the majority of the table. No foreign key -- see
        # this docstring.
        sa.Column("generation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        sa.CheckConstraint("model <> ''", name="ck_llm_calls_model_not_empty"),
        sa.CheckConstraint("tokens_in >= 0", name="ck_llm_calls_tokens_in_non_negative"),
        sa.CheckConstraint("tokens_out >= 0", name="ck_llm_calls_tokens_out_non_negative"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_calls_cost_usd_non_negative"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_llm_calls_latency_ms_non_negative"),
        # `LLMCall._ok_and_error_must_agree`'s two clauses.
        sa.CheckConstraint(
            "(ok AND error IS NULL) OR (NOT ok AND error IS NOT NULL AND error <> '')",
            name="ck_llm_calls_ok_error_agree",
        ),
    )
    # No index on `llm_calls` beyond its primary key. The two that will be
    # right, and the query each serves, are in this migration's docstring.


def downgrade() -> None:
    op.drop_table("llm_calls")
    # **Not load-bearing, and kept anyway so `downgrade()` mirrors `upgrade()` statement
    # for statement and a reader can diff the two by eye.** `op.drop_table` on the next
    # line takes the index with it regardless, so deleting this line is an equivalent
    # mutation -- unlike `ff`'s downgrade, where the `create_index` is the only thing
    # that restores the index and removing it really does leave the schema short.
    op.drop_index("ix_curated_rows_user_newest", table_name="curated_rows")
    op.drop_table("curated_rows")
