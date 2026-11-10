"""The correspondence between `SeedRow` and `seed_rows`, and the bounds the table mirrors."""

import re
from typing import cast

from annotated_types import MaxLen, MinLen
from sqlalchemy import CheckConstraint, Table
from sqlalchemy import Enum as SAEnum
from sqlalchemy.schema import DefaultClause

from usher.db.models.seed_row import SeedRowRow
from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, SEED_ROW_CARDS, SeedRow, SeedRowOutcome

_TABLE = cast(Table, SeedRowRow.__table__)


def _checks() -> dict[str, str]:
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in _TABLE.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _numbers(sql: str) -> set[int]:
    return {int(found) for found in re.findall(r"\d+", sql)}


def test_the_row_holds_the_models_fields_with_the_cards_split_in_two() -> None:
    """The declared exception to the 1:1 rule: `cards` is two parallel arrays.

    `card_title_ids` and `card_sources` replace it, so an array of composites, a child
    table or a JSON column drops one of them and this names it.
    """
    expected = (set(SeedRow.model_fields) - {"cards"}) | {"card_title_ids", "card_sources"}

    assert {column.name for column in _TABLE.columns} == expected


def test_the_table_carries_no_created_at_or_updated_at() -> None:
    """The record's own timestamps are `built_at` and `attempted_at`.

    An `updated_at` would oblige a trigger, and `test_migrations.py` pins the set of
    trigger tables exactly.
    """
    names = {column.name for column in _TABLE.columns}

    assert "created_at" not in names and "updated_at" not in names


def test_the_seed_is_the_primary_key_and_its_title_is_deleted_with_it() -> None:
    assert [column.name for column in _TABLE.primary_key.columns] == ["seed_title_id"]
    (foreign_key,) = _TABLE.c.seed_title_id.foreign_keys
    assert foreign_key.target_fullname == "titles.id"
    assert foreign_key.ondelete == "CASCADE"


def test_the_two_card_arrays_are_not_null_and_default_to_empty() -> None:
    """Both, because a record with no cards is a shape and not an absence."""
    for name in ("card_title_ids", "card_sources"):
        column = _TABLE.c[name]
        assert column.nullable is False, name
        default = column.server_default
        assert isinstance(default, DefaultClause), name
        assert str(default.arg) == "'{}'", name


def test_the_build_columns_are_nullable_and_the_attempt_columns_are_not() -> None:
    assert {c.name for c in _TABLE.columns if c.nullable} == {"built_at", "built_recipe"}


def test_the_outcome_column_holds_every_member_in_a_varchar_wide_enough() -> None:
    outcome = _TABLE.c.attempt_outcome.type
    assert isinstance(outcome, SAEnum)
    assert outcome.length is not None
    assert set(outcome.enums) == {member.value for member in SeedRowOutcome}
    assert outcome.native_enum is False
    assert outcome.length >= max(len(member.value) for member in SeedRowOutcome)


def test_the_check_constraint_names() -> None:
    """Hand-named, since `NAMING_CONVENTION` has no `ck` entry."""
    assert set(_checks()) == {
        "ck_seed_rows_sources_match_cards",
        "ck_seed_rows_built_iff_cards",
        "ck_seed_rows_card_count",
        "ck_seed_rows_recipe_length",
    }


def test_the_card_count_check_carries_the_domains_bounds() -> None:
    """Zero, the least and the most: imported, so the table cannot drift from the model."""
    assert _numbers(_checks()["ck_seed_rows_card_count"]) == {0, MIN_SEED_ROW_CARDS, SEED_ROW_CARDS}


def test_the_recipe_length_check_carries_the_domains_width() -> None:
    """Read off the field the table mirrors, whose width is private to the domain module."""
    for name in ("attempt_recipe", "built_recipe"):
        metadata = SeedRow.model_fields[name].metadata
        widths = {m.min_length for m in metadata if isinstance(m, MinLen)} | {
            m.max_length for m in metadata if isinstance(m, MaxLen)
        }
        assert len(widths) == 1, f"the premise: {name} has one fixed width"
        assert _numbers(_checks()["ck_seed_rows_recipe_length"]) == widths
