"""Recipes: which device answers to which, and the facts each one starts with."""

import pytest

from usher.domain.enums import HdrFormat
from usher.domain.recipes import RECIPES, Handover, Windows, recipes_for

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


def test_the_infuse_recipe_is_proven_and_no_other_is() -> None:
    assert RECIPES, "the premise: there are recipes to check"
    assert [recipe.key for recipe in RECIPES if recipe.proven] == ["apple_tv.infuse"]


def test_infuse_waits_as_long_as_the_spike_showed_a_wake_takes() -> None:
    windows = next(r for r in RECIPES if r.key == "apple_tv.infuse").windows
    assert windows == Windows(
        waking_seconds=45.0, retry_seconds=2.0, opening_seconds=20.0, starting_seconds=30.0
    )


def test_every_window_is_positive_and_a_retry_fits_inside_the_wake() -> None:
    assert RECIPES, "the premise: there are recipes to check"
    for recipe in RECIPES:
        w = recipe.windows
        assert min(w.waking_seconds, w.retry_seconds, w.opening_seconds, w.starting_seconds) > 0
        assert w.retry_seconds < w.waking_seconds


def test_infuse_needs_no_press_as_spike_one_found_at_the_tv() -> None:
    hands_free = {recipe.key: recipe.hands_free for recipe in RECIPES}
    assert hands_free == {"apple_tv.infuse": True, "apple_tv.airplay": True, "cast.shield": True}


def test_each_recipe_names_its_own_app_and_handover() -> None:
    handed = {recipe.key: (recipe.handover, recipe.app_id) for recipe in RECIPES}
    assert handed == {
        "apple_tv.infuse": (Handover.INFUSE_CALLBACK, "com.firecore.infuse"),
        "apple_tv.airplay": (Handover.AIRPLAY_URL, None),
        "cast.shield": (Handover.CAST_URL, None),
    }


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
