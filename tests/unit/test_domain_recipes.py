"""Recipes: which device answers to which, and the facts each one starts with."""

import pytest

from usher.domain.enums import HdrFormat
from usher.domain.recipes import RECIPES, recipes_for

APPLE_TV_MODEL = "Apple TV 4K (gen 3)"


def _keys(integration: str, model: str | None) -> tuple[str, ...]:
    return tuple(recipe.key for recipe in recipes_for(integration, model))


def test_the_apple_tv_answers_to_infuse_and_airplay_in_that_order() -> None:
    assert _keys("apple_tv", APPLE_TV_MODEL) == ("apple_tv.infuse", "apple_tv.airplay")


def test_music_assistant_s_copy_of_the_apple_tv_answers_to_nothing() -> None:
    assert _keys("music_assistant", "Apple TV 4K Gen3") == ()


def test_an_apple_tv_that_is_not_a_4k_answers_to_nothing() -> None:
    assert _keys("apple_tv", "Apple TV HD") == ()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("SHIELD Android TV", ("cast.shield",)),
        # The boundary: a model that is the prefix and nothing more.
        ("SHIELD", ("cast.shield",)),
        ("Google Nest Mini", ()),
        ("Not a SHIELD", ()),
        (None, ()),
    ],
)
def test_cast_answers_by_model_prefix(model: str | None, expected: tuple[str, ...]) -> None:
    assert _keys("cast", model) == expected


def test_a_device_of_unknown_model_answers_to_nothing() -> None:
    assert _keys("apple_tv", None) == ()


def test_only_the_recipes_passed_are_consulted() -> None:
    without_infuse = tuple(recipe for recipe in RECIPES if recipe.key != "apple_tv.infuse")
    assert len(without_infuse) == len(RECIPES) - 1, "the premise: Infuse was taken out"
    matched = recipes_for("apple_tv", APPLE_TV_MODEL, without_infuse)
    assert tuple(recipe.key for recipe in matched) == ("apple_tv.airplay",)


def test_every_recipe_is_assumed_until_proven_at_the_tv() -> None:
    assert RECIPES, "the premise: there are recipes to check"
    assert [recipe.key for recipe in RECIPES if recipe.proven] == []


def test_infuse_needs_a_press_until_spike_one_says_otherwise() -> None:
    hands_free = {recipe.key: recipe.hands_free for recipe in RECIPES}
    assert hands_free == {"apple_tv.infuse": False, "apple_tv.airplay": True, "cast.shield": True}


def test_infuse_alone_admits_hlg() -> None:
    hdr = {recipe.key: recipe.direct_play.hdr_formats for recipe in RECIPES}
    assert hdr == {
        "apple_tv.infuse": {HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG},
        "apple_tv.airplay": {HdrFormat.DOLBY_VISION, HdrFormat.HDR10},
        "cast.shield": {HdrFormat.HDR10},
    }


def test_recipe_keys_are_unique() -> None:
    keys = [recipe.key for recipe in RECIPES]
    assert len(keys) == len(set(keys)) == 3
