"""The recall prompt, its schema, its parser and the recipe fingerprint."""

import re
from collections.abc import Callable
from typing import Any

import pytest

from usher.ports.errors import PortDataMalformed
from usher.ports.repository import NamedTitle
from usher.services import seed_row_recall
from usher.services.seed_row_recall import (
    CLOSE_LIMIT,
    CLOSE_MIN_VOTES,
    NAMED_COUNT,
    RECALL_SCHEMA,
    RECALL_TEMPERATURE,
    parse_recall,
    recall_prompt,
    seed_row_recipe,
)

_BODY = (
    "\n"
    "Name 25 real films or series a viewer who loved this one would say feel most like\n"
    "it: the same kind of story, protagonist, themes and tone. Do not pick titles just\n"
    "because their name shares a word with it. Do not include the title itself. Give the\n"
    "exact release title in the title field, without the year, and the year in the year\n"
    "field."
)


def _spec_prompt(title_line: str, overview: str, keywords: str) -> str:
    """The spec's block with its three values filled in."""
    return f"{title_line}\nOverview: {overview}\nKeywords: {keywords}\n{_BODY}"


def test_the_numbers_are_the_specs() -> None:
    assert NAMED_COUNT == 25
    assert CLOSE_MIN_VOTES == 500
    assert CLOSE_LIMIT == 100
    assert RECALL_TEMPERATURE == 0.3


def test_the_prompt_is_the_specs_text() -> None:
    prompt = recall_prompt(
        name="BlackBerry",
        year=2023,
        overview="The rise and fall of the first smartphone.",
        keywords=["smartphone", "technology", "canada"],
    )
    assert prompt == _spec_prompt(
        "Title: BlackBerry (2023).",
        "The rise and fall of the first smartphone.",
        "smartphone, technology, canada",
    )


def test_the_prompt_pins_the_claims_the_model_must_honour() -> None:
    prompt = recall_prompt(name="X", year=2000, overview="o", keywords=["k"])
    assert f"Name {NAMED_COUNT} real films or series" in prompt
    assert "Do not include the title itself." in prompt
    assert "Do not pick titles just\nbecause their name shares a word with it." in prompt
    assert "Give the\nexact release title in the title field, without the year," in prompt


def test_the_prompt_asks_for_as_many_titles_as_the_recipe_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed_row_recall, "NAMED_COUNT", NAMED_COUNT + 1)
    prompt = recall_prompt(name="X", year=2000, overview="o", keywords=["k"])
    assert f"Name {NAMED_COUNT + 1} real films or series" in prompt


def test_without_a_year_the_first_line_has_none() -> None:
    prompt = recall_prompt(name="Breaking Bad", year=None, overview="o", keywords=["k"])
    assert prompt.splitlines()[0] == "Title: Breaking Bad."
    assert prompt == _spec_prompt("Title: Breaking Bad.", "o", "k")


def test_without_an_overview_the_line_is_empty() -> None:
    for overview in (None, ""):
        prompt = recall_prompt(name="X", year=2000, overview=overview, keywords=["k"])
        assert prompt.splitlines()[1] == "Overview: "
        assert prompt == _spec_prompt("Title: X (2000).", "", "k")


def test_keywords_are_joined_with_a_comma_and_a_space() -> None:
    prompt = recall_prompt(name="X", year=2000, overview="o", keywords=["a", "b c", "d"])
    assert prompt.splitlines()[2] == "Keywords: a, b c, d"
    assert recall_prompt(name="X", year=2000, overview="o", keywords=[]).splitlines()[2] == (
        "Keywords: "
    )


def test_the_schema_is_a_films_array_of_title_and_year() -> None:
    assert RECALL_SCHEMA == {
        "type": "object",
        "additionalProperties": False,
        "required": ["films"],
        "properties": {
            "films": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "year"],
                    "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
                },
            }
        },
    }


def test_the_schema_sets_no_floor_on_the_array() -> None:
    assert "minItems" not in RECALL_SCHEMA["properties"]["films"]
    assert RECALL_SCHEMA["properties"]["films"]["maxItems"] > NAMED_COUNT


def test_parse_keeps_the_models_order() -> None:
    films = parse_recall(
        {"films": [{"title": "Heat", "year": 1995}, {"title": "Casino", "year": 1995}]}
    )
    assert films == [NamedTitle("Heat", 1995), NamedTitle("Casino", 1995)]


def test_a_year_inside_the_title_is_stripped() -> None:
    films = parse_recall(
        {
            "films": [
                {"title": "The Social Network (2010)", "year": 2010},
                {"title": "Heat (1995)  ", "year": 1995},
                {"title": "Jobs  (2013)", "year": 2013},
            ]
        }
    )
    assert films == [
        NamedTitle("The Social Network", 2010),
        NamedTitle("Heat", 1995),
        NamedTitle("Jobs", 2013),
    ]


@pytest.mark.parametrize(
    "title",
    ["(500) Days of Summer", "1917", "Movie (US)", "Movie (20)", "Movie (20201)", "A (2010) B"],
)
def test_only_a_trailing_four_digit_year_is_stripped(title: str) -> None:
    assert parse_recall({"films": [{"title": title, "year": 2009}]}) == [NamedTitle(title, 2009)]


def test_an_entry_with_an_empty_title_is_skipped() -> None:
    films = parse_recall(
        {
            "films": [
                {"title": "", "year": 2000},
                {"title": "   ", "year": 2001},
                {"title": "(2002)", "year": 2002},
                {"title": "Heat", "year": 1995},
            ]
        }
    )
    assert films == [NamedTitle("Heat", 1995)]


def test_the_first_of_a_duplicate_name_and_year_is_kept() -> None:
    films = parse_recall(
        {
            "films": [
                {"title": "Air", "year": 2023},
                {"title": "AIR (2023)", "year": 2023},
                {"title": "air", "year": 2023},
                {"title": "Air", "year": 1993},
            ]
        }
    )
    assert films == [NamedTitle("Air", 2023), NamedTitle("Air", 1993)]


def test_an_entry_that_is_not_a_title_and_a_year_is_skipped() -> None:
    films = parse_recall(
        {
            "films": [
                "Heat",
                {"title": "Casino"},
                {"title": 7, "year": 1995},
                {"title": "Jobs", "year": "2013"},
                {"title": "Alien", "year": True},
                {"title": "Sinners", "year": 2025},
            ]
        }
    )
    assert films == [NamedTitle("Sinners", 2025)]


def test_an_empty_films_list_parses_to_nothing() -> None:
    assert parse_recall({"films": []}) == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"movies": []}, {"films": None}, {"films": "Heat"}, {"films": {"title": "Heat"}}],
)
def test_a_payload_without_films_is_malformed(payload: dict[str, Any]) -> None:
    with pytest.raises(PortDataMalformed, match="films"):
        parse_recall(payload)


def test_the_recipe_is_thirty_two_hex_characters_and_stable() -> None:
    recipe = seed_row_recipe(model="gemma")
    assert re.fullmatch(r"[0-9a-f]{32}", recipe)
    assert seed_row_recipe(model="gemma") == recipe


def test_the_recipe_changes_with_the_model_and_with_any_merge_constant() -> None:
    assert seed_row_recipe(model="gemma") != seed_row_recipe(model="qwen")


_CHANGES: list[tuple[str, Callable[[Any], Any]]] = [
    ("RRF_K", lambda v: v + 1),
    ("SEED_ROW_CARDS", lambda v: v + 1),
    ("MIN_SEED_ROW_CARDS", lambda v: v + 1),
    ("CLOSE_MIN_VOTES", lambda v: v + 1),
    ("CLOSE_LIMIT", lambda v: v + 1),
    ("NAMED_COUNT", lambda v: v + 1),
    ("RECALL_TEMPERATURE", lambda v: v + 0.1),
    ("RECALL_TEMPLATE", lambda v: v + " "),
    ("RECALL_SCHEMA", lambda v: {**v, "description": "changed"}),
]


@pytest.mark.parametrize(("name", "change"), _CHANGES, ids=[name for name, _ in _CHANGES])
def test_the_recipe_changes_with_each_constant_it_hashes(
    name: str, change: Callable[[Any], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = seed_row_recipe(model="gemma")
    original = getattr(seed_row_recall, name)
    changed = change(original)
    assert changed != original
    monkeypatch.setattr(seed_row_recall, name, changed)
    assert seed_row_recipe(model="gemma") != before
