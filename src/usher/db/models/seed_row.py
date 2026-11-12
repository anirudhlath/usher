"""`seed_rows` -- one stored "Because you watched" row per seed title."""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, SEED_ROW_CARDS, SeedRowOutcome

#: The width of a recipe fingerprint, which `SeedRow` holds as a private constant. The
#: migration writes the number itself, and `tests/unit/test_db_models_seed_row.py` reads
#: it back off the domain field, so a change to either side fails there.
RECIPE_LENGTH = 32


class SeedRowRow(Base):
    """What a seed's row holds, and how its latest try ended.

    **Keyed by the seed title, not by user:** the row is the same for anyone who watched
    the seed. The cards are two parallel arrays rather than a child table, because the
    order is the product and a row is read whole -- `curated_rows.card_title_ids`'
    argument -- and `card_sources` carries one entry per card, each its source names
    sorted and joined by `+` (`close+named`).

    `card_title_ids`, `built_at` and `built_recipe` describe the last good build and are
    all present or all absent; `attempted_at`, `attempt_outcome` and `attempt_recipe`
    describe the latest try, so a failed rebuild keeps the old cards and still records
    that it failed. The columns are `SeedRow`'s fields with `cards` split in two.
    """

    __tablename__ = "seed_rows"

    # The primary key *is* the foreign key: one record per seed. CASCADE, for
    # `title_neighbors`' reason: a record protects no state a delete would destroy,
    # since the scheduler builds it again. The key leads the only lookup and the
    # cascade's own, so no index is owed beyond it.
    seed_title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    # `uuid[]`, ordered. No foreign key is possible over array elements; a card whose
    # title is deleted later is dropped when the row is read.
    card_title_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    card_sources: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    built_recipe: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_outcome: Mapped[SeedRowOutcome] = mapped_column(
        enum_column(SeedRowOutcome, length=16), nullable=False
    )
    attempt_recipe: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # One source entry per card: a pairing by position that a length mismatch breaks.
        CheckConstraint(
            "cardinality(card_sources) = cardinality(card_title_ids)",
            name="ck_seed_rows_sources_match_cards",
        ),
        # `SeedRow._a_build_is_cards_time_and_recipe_together`: the three describing the
        # last good build are present together or not at all.
        CheckConstraint(
            "(cardinality(card_title_ids) > 0) = (built_at IS NOT NULL) "
            "AND (built_at IS NOT NULL) = (built_recipe IS NOT NULL)",
            name="ck_seed_rows_built_iff_cards",
        ),
        # No cards, or between the least a row needs and the most it shows. The
        # bounds are the domain's own, imported so the model cannot drift from it.
        CheckConstraint(
            f"cardinality(card_title_ids) = 0 OR (cardinality(card_title_ids) >= "
            f"{MIN_SEED_ROW_CARDS} AND cardinality(card_title_ids) <= {SEED_ROW_CARDS})",
            name="ck_seed_rows_card_count",
        ),
        # Both fingerprints are fixed-width, the build's only when there is one.
        CheckConstraint(
            f"(built_recipe IS NULL OR length(built_recipe) = {RECIPE_LENGTH}) "
            f"AND length(attempt_recipe) = {RECIPE_LENGTH}",
            name="ck_seed_rows_recipe_length",
        ),
    )
