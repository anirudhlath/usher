"""Ranking: every recipe against real `media_items` shapes, and the order a screen shows."""

import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from tests.fakes.house import APPLE_TV, BEDROOM, LIVING_ROOM, SHIELD_DEVICE, this_house
from usher.domain.enums import HdrFormat
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.recipes import RECIPES, Recipe
from usher.domain.screens import Screen, ScreenDevice, project
from usher.domain.source import MediaItem
from usher.domain.ways import (
    Resolution,
    facts_known,
    gives_up,
    quality_of,
    rank,
    refusal,
    resolution_of,
    way_id,
)

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TITLE = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
RECIPE = {recipe.key: recipe for recipe in RECIPES}
# Names whose codepoint, casefolded and reading orders all disagree, listed in none of them.
_READ_APART = ("Pantry", "Écran", "office")
# One tier per row, best first; every codec in a row scores alike.
_AUDIO_LADDER: tuple[tuple[str | None, ...], ...] = (
    ("truehd",),
    ("dts", "flac", "pcm_s24le"),
    ("eac3",),
    ("ac3",),
    ("aac", "opus", "mp3"),
    (None, "vorbis"),
)


def _copy(name: str, **fields: Any) -> MediaItem:
    return MediaItem(source_id=SOURCE, title_id=TITLE, external_id=name, **fields)


DV_MKV = _copy(
    "dv-mkv",
    container="mkv",
    video_codec="hevc",
    hdr_format=HdrFormat.DOLBY_VISION,
    audio_codec="truehd",
    audio_channels=8,
    width=3840,
    height=2160,
)
# ffprobe's spelling of an MP4: one container under six names.
HDR10_MP4 = _copy(
    "hdr10-mp4",
    container="mov,mp4,m4a,3gp,3g2,mj2",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="eac3",
    audio_channels=6,
    width=3840,
    height=2160,
)
H264_BARE = _copy("h264-bare", video_codec="h264")
UNKNOWN = _copy("unknown", container="mkv")
HD_MKV = _copy(
    "hd-mkv",
    container="mkv",
    video_codec="h264",
    audio_codec="ac3",
    audio_channels=6,
    width=1920,
    height=1080,
)


def _shield_in_the_bedroom(recipes: tuple[Recipe, ...] = RECIPES) -> tuple[Screen, ...]:
    house: HouseSnapshot = this_house(shield=DeviceRole.USE)
    house = house.with_registries(
        devices=tuple(
            replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
            for one in house.devices
        )
    )
    return project(house, recipes).screens


def _living_room() -> Screen:
    screen = project(this_house()).screen(LIVING_ROOM)
    assert screen is not None
    return screen


def _unaccented(name: str) -> str:
    """Reading order's key spelled apart from `reading_key`, for premises: ASCII letters only."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().casefold()


def _assert_read_apart(names: Sequence[str]) -> None:
    orders = {
        "codepoint": sorted(names),
        "casefold": sorted(names, key=str.casefold),
        "reading": sorted(names, key=_unaccented),
    }
    assert len({tuple(order) for order in orders.values()}) == 3, (
        f"the premise: codepoint, casefold and reading orders disagree: {orders}"
    )
    assert list(names) not in orders.values(), f"the premise: {names} is in none of {orders}"


def _tall_enough(recipe_key: str, *copies: MediaItem) -> None:
    """The premise of a case whose answer needs no copy refused for its height."""
    recipe = RECIPE[recipe_key]
    ceiling = recipe.direct_play.max_height
    tallest = max(copy.height or 0 for copy in copies)
    assert tallest <= ceiling, f"the premise: {recipe.name} plays {tallest} lines, up to {ceiling}"


def _shield(entity_id: str, name: str, area_id: str) -> ScreenDevice:
    """A confirmed SHIELD built by hand, so a case chooses where its screen lists it."""
    return ScreenDevice(
        entity_id=entity_id,
        name=name,
        integration="cast",
        model="SHIELD Android TV",
        area_id=area_id,
        device_id=None,
        role=DeviceRole.USE,
        recipes=(RECIPE["cast.shield"],),
        state=None,
    )


@pytest.mark.parametrize(
    ("recipe", "item", "expected_refusal", "expected_known"),
    [
        ("apple_tv.infuse", DV_MKV, None, True),
        ("apple_tv.infuse", HDR10_MP4, None, True),
        ("apple_tv.infuse", H264_BARE, None, False),
        ("apple_tv.infuse", UNKNOWN, None, False),
        ("apple_tv.airplay", DV_MKV, "MKV OVER AIRPLAY", True),
        ("apple_tv.airplay", HDR10_MP4, None, True),
        ("apple_tv.airplay", H264_BARE, None, False),
        ("apple_tv.airplay", UNKNOWN, "MKV OVER AIRPLAY", False),
        ("cast.shield", DV_MKV, "NO DOLBY VISION OVER CAST", True),
        ("cast.shield", HDR10_MP4, None, True),
        ("cast.shield", H264_BARE, None, False),
        ("cast.shield", UNKNOWN, None, False),
    ],
)
def test_every_recipe_against_every_copy_shape(
    recipe: str, item: MediaItem, expected_refusal: str | None, expected_known: bool
) -> None:
    _tall_enough(recipe, item)
    assert refusal(item, RECIPE[recipe]) == expected_refusal
    assert facts_known(item, RECIPE[recipe]) is expected_known


def test_audio_outside_the_profile_is_refused_by_name() -> None:
    item = HDR10_MP4.evolve(audio_codec="truehd")
    assert refusal(item, RECIPE["apple_tv.airplay"]) == "TRUEHD OVER AIRPLAY"


def test_a_copy_taller_than_the_profile_is_refused_by_height() -> None:
    cast = RECIPE["cast.shield"]
    capped = replace(cast, direct_play=replace(cast.direct_play, max_height=1080))
    assert refusal(HDR10_MP4, cast) is None, "the premise: only the height differs"
    assert refusal(HDR10_MP4, capped) == "2160P OVER CAST"


def test_a_source_s_capitals_read_as_the_profile_s_lowercase() -> None:
    lower = HDR10_MP4.evolve(container="mp4")
    capitals = lower.evolve(container="MP4", video_codec="HEVC", audio_codec="EAC3")
    assert refusal(lower, RECIPE["apple_tv.airplay"]) is None, "the premise: lowercase plays"
    assert refusal(capitals, RECIPE["apple_tv.airplay"]) is None
    assert quality_of(capitals).score == quality_of(lower).score


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 1600, Resolution.UHD_2160),
        (3200, None, Resolution.UHD_2160),
        (3199, None, Resolution.HD_1080),
        (None, 1800, Resolution.UHD_2160),
        (None, 1799, Resolution.HD_1080),
        (1920, 800, Resolution.HD_1080),
        (1800, None, Resolution.HD_1080),
        (1799, None, Resolution.HD_720),
        (None, 900, Resolution.HD_1080),
        (None, 899, Resolution.HD_720),
        (1200, None, Resolution.HD_720),
        (1199, None, Resolution.SD),
        (None, 650, Resolution.HD_720),
        (None, 649, Resolution.SD),
        (1199, 649, Resolution.SD),
        (720, 480, Resolution.SD),
        (None, None, Resolution.UNKNOWN),
    ],
)
def test_resolution_is_classed_by_either_dimension(
    width: int | None, height: int | None, expected: Resolution
) -> None:
    assert resolution_of(width, height) is expected


def test_the_score_orders_resolution_before_hdr_before_audio() -> None:
    hd_everything = _copy(
        "hd", width=1920, height=1080, hdr_format=HdrFormat.DOLBY_VISION, audio_codec="truehd"
    )
    uhd_plain = _copy("uhd", width=3840, height=2160, audio_codec="aac")
    uhd_hdr = uhd_plain.evolve(hdr_format=HdrFormat.HDR10)
    uhd_lossless = uhd_plain.evolve(audio_codec="truehd")
    assert quality_of(hd_everything).score < quality_of(uhd_plain).score
    assert quality_of(uhd_lossless).score < quality_of(uhd_hdr).score


def test_the_score_orders_audio_before_channels_before_container() -> None:
    uhd = _copy("uhd", width=3840, height=2160)
    eac3_stereo_mp4 = uhd.evolve(audio_codec="eac3", audio_channels=2, container="mp4")
    ac3_eight_mp4 = uhd.evolve(audio_codec="ac3", audio_channels=8, container="mp4")
    ac3_six_mkv = uhd.evolve(audio_codec="ac3", audio_channels=6, container="mkv")
    ac3_six_mp4 = ac3_six_mkv.evolve(container="mp4")
    assert quality_of(ac3_eight_mp4).score < quality_of(eac3_stereo_mp4).score
    assert quality_of(ac3_six_mkv).score < quality_of(ac3_eight_mp4).score
    assert quality_of(ac3_six_mp4).score < quality_of(ac3_six_mkv).score


def test_an_mp4_under_ffprobe_s_six_names_scores_as_an_mp4() -> None:
    assert quality_of(HDR10_MP4).score == quality_of(HDR10_MP4.evolve(container="mp4")).score


def test_hdr_ranks_dolby_vision_then_hdr10_then_hlg_then_none() -> None:
    sdr = _copy("sdr", width=3840, height=2160, audio_codec="eac3")
    formats = (HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG, None)
    scores = [quality_of(sdr.evolve(hdr_format=hdr)).score for hdr in formats]
    assert [higher > lower for higher, lower in pairwise(scores)] == [True, True, True]


def test_audio_ranks_lossless_object_audio_first_and_unknown_audio_last() -> None:
    tiers = [
        {quality_of(_copy("audio", audio_codec=codec)).score for codec in tier}
        for tier in _AUDIO_LADDER
    ]
    assert [len(tier) for tier in tiers] == [1] * len(_AUDIO_LADDER), "each row scores alike"
    heads = [min(tier) for tier in tiers]
    assert [higher > lower for higher, lower in pairwise(heads)] == [True] * (len(heads) - 1)


def test_a_retracted_copy_is_no_way_at_all() -> None:
    living_room = _living_room()
    assert len(rank((living_room,), (HDR10_MP4,))) == 2, "the premise: available, it has ways"
    assert rank((living_room,), (HDR10_MP4.evolve(available=False),)) == ()


def test_hands_free_ways_rank_above_ways_that_need_a_press_whatever_their_quality() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    assert quality_of(DV_MKV).score > quality_of(HDR10_MP4).score, "the premise"
    ways = rank((_living_room(),), (DV_MKV, HDR10_MP4))
    assert [(way.recipe.key, way.file.external_id, way.playable) for way in ways] == [
        ("apple_tv.airplay", "hdr10-mp4", True),
        ("apple_tv.infuse", "dv-mkv", True),
        ("apple_tv.infuse", "hdr10-mp4", True),
        ("apple_tv.airplay", "dv-mkv", False),
    ]


def test_best_is_the_first_playable_way_and_the_rest_say_what_they_give_up() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (DV_MKV, HDR10_MP4))
    assert [way.best for way in ways] == [True, False, False, False]
    assert [way.gives_up for way in ways] == [
        (),
        ("NEEDS A PRESS",),
        ("NEEDS A PRESS",),
        (),
    ]
    assert ways[-1].refusal == "MKV OVER AIRPLAY"


def test_gives_up_names_resolution_hdr_and_audio_against_the_best() -> None:
    bedroom = _shield_in_the_bedroom()[0]
    best_copy = _copy(
        "best",
        container="mkv",
        video_codec="hevc",
        hdr_format=HdrFormat.HDR10,
        audio_codec="truehd",
        width=3840,
        height=2160,
    )
    lesser = _copy(
        "lesser", container="mp4", video_codec="h264", audio_codec="aac", width=1920, height=1080
    )
    _tall_enough("cast.shield", lesser, best_copy)
    best, other = rank((bedroom,), (lesser, best_copy))
    assert best.file.external_id == "best", "the premise: the ranking chose the 4K copy"
    assert gives_up(other, best) == ("1080P", "NO HDR10", "NO TRUEHD")
    assert other.gives_up == ("1080P", "NO HDR10", "NO TRUEHD")


def test_against_a_best_that_needs_a_press_no_way_gives_the_press_up() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (HD_MKV, DV_MKV))
    assert [(way.recipe.key, way.file.external_id, way.best) for way in ways] == [
        ("apple_tv.infuse", "dv-mkv", True),
        ("apple_tv.infuse", "hd-mkv", False),
        ("apple_tv.airplay", "dv-mkv", False),
        ("apple_tv.airplay", "hd-mkv", False),
    ]
    assert [way.gives_up for way in ways] == [
        (),
        ("1080P", "NO DOLBY VISION", "NO TRUEHD"),
        (),
        (),
    ]


def test_each_way_carries_its_own_id_and_whether_its_facts_are_known() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (H264_BARE, HDR10_MP4))
    assert [(way.recipe.key, way.file.external_id) for way in ways] == [
        ("apple_tv.airplay", "hdr10-mp4"),
        ("apple_tv.airplay", "h264-bare"),
        ("apple_tv.infuse", "hdr10-mp4"),
        ("apple_tv.infuse", "h264-bare"),
    ]
    assert [way.facts_known for way in ways] == [True, False, True, False]
    assert [way.id for way in ways] == [
        way_id(LIVING_ROOM, APPLE_TV, way.recipe.key, way.file.id) for way in ways
    ]
    unknown = ("UNKNOWN RESOLUTION", "NO HDR10", "NO EAC3")
    assert [way.gives_up for way in ways] == [
        (),
        unknown,
        ("NEEDS A PRESS",),
        (*unknown, "NEEDS A PRESS"),
    ]


def test_equal_scores_put_a_proven_recipe_first() -> None:
    """Keys chosen so the key tie-break alone would put the assumed one first."""
    cast = RECIPE["cast.shield"]
    assumed = replace(cast, key="a.assumed", proven=False)
    proven = replace(cast, key="b.proven", proven=True)
    assert assumed.key < proven.key, "the premise"
    screens = _shield_in_the_bedroom((assumed, proven))
    ways = rank(screens[:1], (HDR10_MP4,))
    assert [way.recipe.key for way in ways] == ["b.proven", "a.assumed"]


@pytest.mark.parametrize("reverse", [False, True], ids=["in_id_order", "against_id_order"])
def test_equal_ways_fall_to_the_copy_id_whatever_order_the_copies_come_in(reverse: bool) -> None:
    first = HDR10_MP4.evolve(id=uuid.UUID(int=1), external_id="first")
    second = HDR10_MP4.evolve(id=uuid.UUID(int=2), external_id="second")
    assert str(first.id) < str(second.id), "the premise"
    _tall_enough("cast.shield", first, second)
    files = (second, first) if reverse else (first, second)
    ways = rank(_shield_in_the_bedroom()[:1], files)
    assert [(way.file.external_id, way.best) for way in ways] == [
        ("first", True),
        ("second", False),
    ]


def test_ways_are_grouped_by_screen_and_each_screen_has_its_own_best() -> None:
    screens = _shield_in_the_bedroom()
    assert [screen.name for screen in screens] == ["Bedroom", "Living Room"], "the premise"
    _tall_enough("cast.shield", HDR10_MP4)
    ways = rank(tuple(reversed(screens)), (HDR10_MP4,))
    assert [(way.screen.area_id, way.best) for way in ways] == [
        (BEDROOM, True),
        (LIVING_ROOM, True),
        (LIVING_ROOM, False),
    ]


def test_screens_are_ranked_as_a_person_reads_not_by_codepoint_or_casefold() -> None:
    _assert_read_apart(_READ_APART)
    screens = tuple(
        Screen(
            area_id=f"area_{index}",
            name=name,
            devices=(_shield(f"media_player.shield_{index}", "SHIELD", f"area_{index}"),),
        )
        for index, name in enumerate(_READ_APART)
    )
    ways = rank(screens, (HDR10_MP4,))
    assert [way.screen.name for way in ways] == ["Écran", "office", "Pantry"]


def test_players_on_one_screen_tie_as_a_person_reads_and_the_first_is_the_best() -> None:
    _assert_read_apart(_READ_APART)
    den = Screen(
        area_id="den",
        name="Den",
        devices=tuple(
            _shield(f"media_player.shield_{index}", name, "den")
            for index, name in enumerate(_READ_APART)
        ),
    )
    _tall_enough("cast.shield", HDR10_MP4)
    ways = rank((den,), (HDR10_MP4,))
    assert [(way.device.name, way.best) for way in ways] == [
        ("Écran", True),
        ("office", False),
        ("Pantry", False),
    ]
    assert [way.gives_up for way in ways] == [(), (), ()]


def test_a_screen_whose_every_way_is_refused_has_no_best() -> None:
    avi = _copy("avi", container="avi", video_codec="mpeg4")
    ways = rank((_living_room(),), (avi,))
    assert [way.refusal for way in ways] == ["AVI OVER AIRPLAY", "AVI OVER INFUSE"]
    assert not any(way.best for way in ways)


@pytest.mark.parametrize("changed", ["area", "entity", "recipe", "copy"])
def test_a_way_id_is_stable_and_differs_when_any_part_differs(changed: str) -> None:
    parts: dict[str, Any] = {
        "area": "living_room",
        "entity": "media_player.a",
        "recipe": "cast.shield",
        "copy": TITLE,
    }
    original = way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"])
    assert original == way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"])
    parts[changed] = SOURCE if changed == "copy" else f"{parts[changed]}-other"
    assert way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"]) != original
