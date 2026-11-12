"""A seed title's stored row, and the states it may not reach."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)

_SEED = uuid.UUID("20000000-0000-7000-8000-000000000001")
_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
_RECIPE = "0123456789abcdef0123456789abcdef"
_OTHER_RECIPE = "fedcba9876543210fedcba9876543210"


def _cards(count: int) -> tuple[SeedRowCard, ...]:
    return tuple(
        SeedRowCard(
            title_id=uuid.UUID(int=(0x30 << 120) + n), sources=frozenset({CardSource.NAMED})
        )
        for n in range(count)
    )


def _built(**overrides: object) -> SeedRow:
    fields: dict[str, object] = {
        "seed_title_id": _SEED,
        "cards": _cards(MIN_SEED_ROW_CARDS),
        "built_at": _NOW,
        "built_recipe": _RECIPE,
        "attempted_at": _NOW,
        "attempt_outcome": SeedRowOutcome.BUILT,
        "attempt_recipe": _RECIPE,
    }
    return SeedRow(**(fields | overrides))


def _failed(outcome: SeedRowOutcome, **overrides: object) -> SeedRow:
    """A row whose latest try failed and which has no good build behind it."""
    fields: dict[str, object] = {
        "seed_title_id": _SEED,
        "attempted_at": _NOW,
        "attempt_outcome": outcome,
        "attempt_recipe": _RECIPE,
    }
    return SeedRow(**(fields | overrides))


def test_the_constants_are_the_specs_numbers() -> None:
    assert (SEED_ROW_CARDS, MIN_SEED_ROW_CARDS) == (20, 8)


def test_the_vocabulary_is_the_neutral_one() -> None:
    assert {s.value for s in CardSource} == {"named", "recommended", "close"}
    assert {o.value for o in SeedRowOutcome} == {"built", "too_few", "unusable"}


def test_a_built_row_reads_back_what_it_was_given() -> None:
    cards = _cards(10)
    row = _built(cards=cards)
    assert row.cards == cards
    assert row.seed_title_id == _SEED
    assert row.built_at == _NOW
    assert row.built_recipe == _RECIPE
    assert row.attempted_at == _NOW
    assert row.attempt_outcome is SeedRowOutcome.BUILT
    assert row.attempt_recipe == _RECIPE


def test_a_card_needs_at_least_one_source() -> None:
    with pytest.raises(ValidationError, match="sources"):
        SeedRowCard(title_id=uuid.uuid4(), sources=frozenset())


def test_a_card_keeps_every_source_it_was_found_by() -> None:
    sources = frozenset({CardSource.NAMED, CardSource.CLOSE})
    assert SeedRowCard(title_id=uuid.uuid4(), sources=sources).sources == sources


def test_a_failed_attempt_with_no_build_behind_it_has_no_cards() -> None:
    for outcome in (SeedRowOutcome.TOO_FEW, SeedRowOutcome.UNUSABLE):
        row = _failed(outcome)
        assert row.cards == ()
        assert row.built_at is None
        assert row.built_recipe is None


def test_a_failed_attempt_keeps_the_last_good_build() -> None:
    later = _NOW + timedelta(days=30)
    row = _built(
        attempted_at=later,
        attempt_outcome=SeedRowOutcome.TOO_FEW,
        attempt_recipe=_OTHER_RECIPE,
    )
    assert len(row.cards) == MIN_SEED_ROW_CARDS
    assert row.built_at == _NOW
    assert row.built_recipe == _RECIPE
    assert row.attempted_at == later
    assert row.attempt_recipe == _OTHER_RECIPE


def test_cards_without_a_build_time_are_refused() -> None:
    with pytest.raises(ValidationError, match="cards, built_at and built_recipe"):
        _built(built_at=None, built_recipe=None, attempt_outcome=SeedRowOutcome.TOO_FEW)
    with pytest.raises(ValidationError, match="cards, built_at and built_recipe"):
        _built(built_at=None, attempt_outcome=SeedRowOutcome.TOO_FEW)


def test_a_build_time_without_cards_is_refused() -> None:
    with pytest.raises(ValidationError, match="cards, built_at and built_recipe"):
        _failed(SeedRowOutcome.TOO_FEW, built_at=_NOW, built_recipe=_RECIPE)


def test_a_build_recipe_without_cards_is_refused() -> None:
    with pytest.raises(ValidationError, match="cards, built_at and built_recipe"):
        _failed(SeedRowOutcome.TOO_FEW, built_recipe=_RECIPE)


@pytest.mark.parametrize("count", [MIN_SEED_ROW_CARDS, SEED_ROW_CARDS])
def test_a_row_may_hold_the_minimum_and_the_maximum(count: int) -> None:
    assert len(_built(cards=_cards(count)).cards) == count


@pytest.mark.parametrize("count", [1, MIN_SEED_ROW_CARDS - 1, SEED_ROW_CARDS + 1])
def test_a_row_outside_the_card_range_is_refused(count: int) -> None:
    with pytest.raises(ValidationError, match="between 8 and 20 cards"):
        _built(cards=_cards(count))


def test_the_range_error_names_the_count_it_saw() -> None:
    with pytest.raises(ValidationError, match="not 7"):
        _built(cards=_cards(MIN_SEED_ROW_CARDS - 1))


def test_a_title_may_not_be_a_card_twice() -> None:
    cards = _cards(MIN_SEED_ROW_CARDS)
    with pytest.raises(ValidationError, match="distinct"):
        _built(cards=(*cards, cards[3]))


def test_the_seed_may_not_be_its_own_card() -> None:
    cards = _cards(MIN_SEED_ROW_CARDS - 1)
    own = SeedRowCard(title_id=_SEED, sources=frozenset({CardSource.RECOMMENDED}))
    with pytest.raises(ValidationError, match="seed"):
        _built(cards=(*cards, own))


def test_a_built_attempt_must_carry_cards() -> None:
    with pytest.raises(ValidationError, match="built attempt"):
        _failed(SeedRowOutcome.BUILT)


def test_a_built_attempt_must_be_the_build() -> None:
    later = _NOW + timedelta(minutes=1)
    assert later != _NOW
    with pytest.raises(ValidationError, match="built attempt"):
        _built(attempted_at=later)


def test_a_built_attempt_must_use_the_build_recipe() -> None:
    assert _OTHER_RECIPE != _RECIPE
    with pytest.raises(ValidationError, match="built attempt"):
        _built(attempt_recipe=_OTHER_RECIPE)


@pytest.mark.parametrize("recipe", ["", "abc", _RECIPE + "0"])
def test_the_attempt_recipe_is_thirty_two_characters(recipe: str) -> None:
    assert len(recipe) != 32
    with pytest.raises(ValidationError, match="attempt_recipe"):
        _failed(SeedRowOutcome.UNUSABLE, attempt_recipe=recipe)


@pytest.mark.parametrize("recipe", ["", "abc", _RECIPE + "0"])
def test_the_build_recipe_is_thirty_two_characters(recipe: str) -> None:
    assert len(recipe) != 32
    with pytest.raises(ValidationError, match="built_recipe"):
        _built(built_recipe=recipe, attempt_recipe=recipe)


def test_evolve_revalidates_a_row() -> None:
    row = _built()
    assert len(row.evolve(cards=_cards(SEED_ROW_CARDS)).cards) == SEED_ROW_CARDS
    with pytest.raises(ValidationError, match="between 8 and 20 cards"):
        row.evolve(cards=_cards(3))
