"""`curated_rows` and `llm_calls` — what a generation produced, and what it cost."""

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
    # `curated-1`, `curated-2`, … zero-padded to the width of the generation, so ten
    # rows are `curated-01` … `curated-10`.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    # The model's own prose, rendered as the shelf heading -- the one string in this
    # schema that a language model wrote and a user reads verbatim, which is why
    # ADR-0028's validator is the only thing between the two.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable, and reachable: none of M7's nine providers can produce a row
    # with nothing to explain, and a model that returns an empty reason
    # should give a row with no subtitle rather than a row with an empty one.
    reason: Mapped[str | None] = mapped_column(Text)
    # `uuid[]`, ordered, and the order is the product.
    card_title_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    # The model's own ordering of the rows within one generation, `ge=0` because it
    # indexes the list the model returned.
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
    # **No `server_default`, unlike every other timestamp in this schema.** This is one
    # instant per *generation*, written identically onto every row of it, which is what
    # makes `ORDER BY generated_at DESC` select a whole generation instead of a mixture.
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # **The read.** `list_for_user` is `WHERE user_id = :user_id` ordered by the
        # newest `generated_at`; `replace_for_user`'s `DELETE` is the same lookup; and
        # Postgres implements `users`' `ON DELETE CASCADE` by finding referencing rows
        # *by this column*, which is `ix_media_items_episode_id`'s argument verbatim.
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
    """One *attempted* completion, whether or not it worked — PRD 10's cost ledger."""

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
    # `time.monotonic()` across the whole request -- transport and decode included, not
    # the provider's own reported generation time.
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
        # `LLMCall._ok_and_error_must_agree`'s two clauses, in SQL.
        CheckConstraint(
            "(ok AND error IS NULL) OR (NOT ok AND error IS NOT NULL AND error <> '')",
            name="ck_llm_calls_ok_error_agree",
        ),
        # **The two `m08a` deferred, landed by `m10c`** -- and the deferral is
        # discharged rather than forgotten.
        Index("ix_llm_calls_at", "at"),
        # `(generation_id)` serves dashboard 5's "cost per curated row", joining
        # `curated_rows`.
        Index(
            "ix_llm_calls_generation_id",
            "generation_id",
            postgresql_where=text("generation_id IS NOT NULL"),
        ),
        # Not indexed even now: `purpose` and `model`. A deployment holds
        # one or two values of each, so a btree over either is a structure
        # with two entries -- `title_embeddings.model_name`'s refusal, one
        # module over.
    )
