"""A seed title's stored row: the cards its last good build chose, and its latest try."""

import uuid
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from usher.domain.base import DomainModel

SEED_ROW_CARDS = 20
MIN_SEED_ROW_CARDS = 8
_RECIPE_LENGTH = 32


class CardSource(StrEnum):
    """Which candidate list found a card. Neutral names: nothing here is a provider's word."""

    NAMED = "named"
    RECOMMENDED = "recommended"
    CLOSE = "close"


class SeedRowOutcome(StrEnum):
    """How an attempt to build a row ended."""

    BUILT = "built"
    TOO_FEW = "too_few"
    UNUSABLE = "unusable"


class SeedRowCard(DomainModel):
    """One title in a row, and every list that found it."""

    title_id: uuid.UUID
    sources: frozenset[CardSource] = Field(min_length=1)


class SeedRow(DomainModel):
    """What a seed title's "Because you watched" row holds.

    `cards`, `built_at` and `built_recipe` describe the last good build and are all
    present or all absent. The `attempt_*` fields describe the latest try, so a failed
    rebuild keeps the old cards and still records that it failed.
    """

    seed_title_id: uuid.UUID
    cards: tuple[SeedRowCard, ...] = ()
    built_at: AwareDatetime | None = None
    built_recipe: str | None = Field(
        default=None, min_length=_RECIPE_LENGTH, max_length=_RECIPE_LENGTH
    )
    attempted_at: AwareDatetime
    attempt_outcome: SeedRowOutcome
    attempt_recipe: str = Field(min_length=_RECIPE_LENGTH, max_length=_RECIPE_LENGTH)

    @model_validator(mode="after")
    def _a_build_is_cards_time_and_recipe_together(self) -> Self:
        present = (bool(self.cards), self.built_at is not None, self.built_recipe is not None)
        if len(set(present)) != 1:
            raise ValueError("cards, built_at and built_recipe are present together or not at all")
        return self

    @model_validator(mode="after")
    def _a_stored_row_has_between_the_minimum_and_the_maximum_cards(self) -> Self:
        if self.cards and not MIN_SEED_ROW_CARDS <= len(self.cards) <= SEED_ROW_CARDS:
            raise ValueError(
                f"a row holds between {MIN_SEED_ROW_CARDS} and {SEED_ROW_CARDS} cards, "
                f"not {len(self.cards)}"
            )
        return self

    @model_validator(mode="after")
    def _the_cards_are_distinct_and_never_the_seed(self) -> Self:
        ids = [card.title_id for card in self.cards]
        if len(set(ids)) != len(ids):
            raise ValueError("card title ids must be distinct")
        if self.seed_title_id in ids:
            raise ValueError("the seed title may not be one of its own cards")
        return self

    @model_validator(mode="after")
    def _a_built_attempt_is_the_build(self) -> Self:
        """A built attempt is the build; a build time implies cards, by the first validator."""
        if self.attempt_outcome is SeedRowOutcome.BUILT and (
            self.built_at != self.attempted_at or self.built_recipe != self.attempt_recipe
        ):
            raise ValueError(
                "a built attempt must have built_at equal to attempted_at, "
                "built_recipe equal to attempt_recipe, and so cards"
            )
        return self


__all__ = [
    "MIN_SEED_ROW_CARDS",
    "SEED_ROW_CARDS",
    "CardSource",
    "SeedRow",
    "SeedRowCard",
    "SeedRowOutcome",
]
