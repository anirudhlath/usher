"""Ranking: every recipe against real `media_items` shapes, and the order a screen shows."""

import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import fields, replace
from itertools import pairwise
from typing import Any

import pytest

from tests.fakes.house import APPLE_TV, BEDROOM, LIVING_ROOM, SHIELD_DEVICE, this_house
from usher.domain.enums import HdrFormat
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.recipes import RECIPES, DirectPlay, Recipe
from usher.domain.screens import Screen, ScreenDevice, project
from usher.domain.source import MediaItem
from usher.domain.ways import (
    _AUDIO_RANK,
    _TIER_SPAN,
    _UNKNOWN_AUDIO_RANK,
    Resolution,
    _audio_rank,
    _codec,
    _parts,
    _video_codec,
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
VARIANTS = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
RECIPE = {recipe.key: recipe for recipe in RECIPES}
# Names whose codepoint, casefolded and reading orders all disagree, listed in none of them.
_READ_APART = ("Pantry", "Écran", "office")
# Audio as (codec, channels), best first; every pair in a row scores alike.
_AUDIO_LADDER: tuple[tuple[tuple[str | None, int], ...], ...] = (
    (("truehd", 8),),
    (("flac", 6), ("pcm_s24le", 6)),
    (("flac", 2),),
    (("dts", 6),),
    (("eac3", 6),),
    (("ac3", 6),),
    (("aac", 6), ("opus", 6), ("mp3", 6)),
    (("vorbis", 6),),
    (("dts", 2),),
    (("aac", 2),),
    ((None, 2),),
)


def _copy(name: str, **fields: Any) -> MediaItem:
    return MediaItem(source_id=SOURCE, title_id=TITLE, external_id=name, **fields)


def _variant(base: MediaItem, name: str, **changes: Any) -> MediaItem:
    """`base` with `changes`, as a copy of its own: its own name and media-item id."""
    variant = base.evolve(id=uuid.uuid5(VARIANTS, name), external_id=name, **changes)
    assert variant.id != base.id, f"the premise: {name} is a copy of its own"
    return variant


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
# An H.264 file with no stream metadata: its extension still names its container.
H264_BARE = _copy("h264-bare", container="mp4", video_codec="h264")
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
AV1_MP4 = _copy(
    "av1-mp4",
    container="mp4",
    video_codec="av1",
    audio_codec="aac",
    audio_channels=2,
    width=1920,
    height=1080,
)
# PCM under one of ffprobe's sample-format spellings.
PCM_MOV = _copy(
    "pcm-mov",
    container="mov",
    video_codec="hevc",
    audio_codec="pcm_s24le",
    audio_channels=6,
    width=1920,
    height=1080,
)
# ffprobe's spelling of an MKV: Matroska's family name beside WebM's.
MATROSKA = _copy(
    "matroska",
    container="matroska,webm",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="eac3",
    audio_channels=6,
    width=3840,
    height=2160,
)
# Every fact known; each gives-up case takes exactly one away or lowers it.
FULL = _copy(
    "full",
    container="mkv",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="truehd",
    audio_channels=8,
    width=3840,
    height=2160,
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


def _shield(
    entity_id: str, name: str, area_id: str, recipes: tuple[Recipe, ...] = (RECIPE["cast.shield"],)
) -> ScreenDevice:
    """A confirmed SHIELD built by hand, so a case chooses where its screen lists it."""
    return ScreenDevice(
        entity_id=entity_id,
        name=name,
        integration="cast",
        model="SHIELD Android TV",
        area_id=area_id,
        device_id=None,
        role=DeviceRole.USE,
        recipes=recipes,
        state=None,
    )


def _audio(codec: str | None, channels: int | None) -> MediaItem:
    return _copy(f"{codec}-{channels}", audio_codec=codec, audio_channels=channels)


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
        ("apple_tv.infuse", AV1_MP4, "AV1 OVER INFUSE", True),
        ("apple_tv.airplay", AV1_MP4, "AV1 OVER AIRPLAY", True),
        ("cast.shield", AV1_MP4, "AV1 OVER CAST", True),
        ("apple_tv.infuse", MATROSKA, None, True),
        ("apple_tv.airplay", MATROSKA, "MKV OVER AIRPLAY", True),
        ("cast.shield", MATROSKA, None, True),
        ("apple_tv.infuse", PCM_MOV, None, True),
        ("apple_tv.airplay", PCM_MOV, "PCM OVER AIRPLAY", True),
    ],
)
def test_every_recipe_against_every_copy_shape(
    recipe: str, item: MediaItem, expected_refusal: str | None, expected_known: bool
) -> None:
    _tall_enough(recipe, item)
    assert refusal(item, RECIPE[recipe]) == expected_refusal
    assert facts_known(item, RECIPE[recipe]) is expected_known


@pytest.mark.parametrize("missing", ["container", "video_codec", "audio_codec", "height"])
def test_a_copy_missing_any_fact_its_profile_constrains_is_not_fully_known(missing: str) -> None:
    airplay = RECIPE["apple_tv.airplay"]
    assert facts_known(HDR10_MP4, airplay) is True, "the premise: with every fact, it is known"
    assert (
        facts_known(_variant(HDR10_MP4, f"without-{missing}", **{missing: None}), airplay) is False
    )


@pytest.mark.parametrize(
    ("missing", "constraint"),
    [
        ("container", "containers"),
        ("video_codec", "video_codecs"),
        ("audio_codec", "audio_codecs"),
    ],
)
def test_a_fact_its_profile_does_not_constrain_is_not_needed(missing: str, constraint: str) -> None:
    airplay = RECIPE["apple_tv.airplay"]
    unconstrained: dict[str, Any] = {constraint: None}
    loose = replace(airplay, direct_play=replace(airplay.direct_play, **unconstrained))
    item = _variant(HDR10_MP4, f"without-{missing}", **{missing: None})
    assert facts_known(item, airplay) is False, "the premise: AirPlay needs the fact"
    assert facts_known(item, loose) is True


def test_audio_outside_the_profile_is_refused_by_name() -> None:
    item = _variant(HDR10_MP4, "truehd-mp4", audio_codec="truehd")
    assert refusal(item, RECIPE["apple_tv.airplay"]) == "TRUEHD OVER AIRPLAY"


def test_a_profile_listing_pcm_admits_a_copy_in_a_pcm_sample_format() -> None:
    airplay = RECIPE["apple_tv.airplay"]
    pcm_only = replace(airplay.direct_play, audio_codecs=frozenset({"pcm"}))
    with_pcm = replace(airplay, key="test.pcm", direct_play=pcm_only)
    aac = _variant(PCM_MOV, "aac-mov", audio_codec="aac")
    _tall_enough("apple_tv.airplay", PCM_MOV)
    assert PCM_MOV.audio_codec == "pcm_s24le", "the premise: the copy names a sample format"
    assert refusal(aac, with_pcm) == "AAC OVER AIRPLAY", "the premise: the profile constrains audio"
    assert refusal(PCM_MOV, with_pcm) is None


def test_every_shipped_profile_entry_reads_as_a_copy_s_fact_is_folded() -> None:
    """An entry no fold yields never matches a copy: `matroska`, `HEVC`, `pcm_s16le`."""
    folds: dict[str, Callable[[str], tuple[str, ...]]] = {
        "containers": _parts,
        "video_codecs": lambda entry: (_video_codec(entry),),
        "audio_codecs": lambda entry: (_codec(entry),),
    }
    string_sets = {one.name for one in fields(DirectPlay) if one.type == frozenset[str] | None}
    assert set(folds) == string_sets, "the premise: every field of names is folded here"
    named = {
        (recipe.key, field, entry)
        for recipe in RECIPES
        for field, fold in folds.items()
        for entry in getattr(recipe.direct_play, field) or ()
    }
    assert {field for _, field, _ in named} == set(folds), "the premise: each field names some"
    unfolded = {(key, field, one) for key, field, one in named if folds[field](one) != (one,)}
    assert unfolded == set()


def test_a_copy_taller_than_the_profile_is_refused_by_height() -> None:
    cast = RECIPE["cast.shield"]
    capped = replace(cast, direct_play=replace(cast.direct_play, max_height=1080))
    assert refusal(HDR10_MP4, cast) is None, "the premise: only the height differs"
    assert refusal(HDR10_MP4, capped) == "2160P OVER CAST"


def test_a_source_s_capitals_read_as_the_profile_s_lowercase() -> None:
    lower = _variant(HDR10_MP4, "lower", container="mp4")
    capitals = _variant(lower, "capitals", container="MP4", video_codec="HEVC", audio_codec="EAC3")
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
    uhd_hdr = _variant(uhd_plain, "uhd-hdr", hdr_format=HdrFormat.HDR10)
    uhd_lossless = _variant(uhd_plain, "uhd-lossless", audio_codec="truehd")
    assert quality_of(hd_everything).score < quality_of(uhd_plain).score
    assert quality_of(uhd_lossless).score < quality_of(uhd_hdr).score


def test_the_score_orders_audio_before_channels_before_container() -> None:
    uhd = _copy("uhd", width=3840, height=2160)
    eac3_six_mp4 = _variant(
        uhd, "eac3-6-mp4", audio_codec="eac3", audio_channels=6, container="mp4"
    )
    ac3_eight_mp4 = _variant(uhd, "ac3-8-mp4", audio_codec="ac3", audio_channels=8, container="mp4")
    ac3_six_mkv = _variant(uhd, "ac3-6-mkv", audio_codec="ac3", audio_channels=6, container="mkv")
    ac3_six_mp4 = _variant(ac3_six_mkv, "ac3-6-mp4", container="mp4")
    assert quality_of(ac3_eight_mp4).score < quality_of(eac3_six_mp4).score
    assert quality_of(ac3_six_mkv).score < quality_of(ac3_eight_mp4).score
    assert quality_of(ac3_six_mp4).score < quality_of(ac3_six_mkv).score


@pytest.mark.parametrize(
    ("family", "named"),
    [
        pytest.param("mov,mp4,m4a,3gp,3g2,mj2", "mp4", id="mp4"),
        pytest.param("matroska,webm", "mkv", id="matroska"),
    ],
)
def test_a_container_family_scores_as_the_container_it_names(family: str, named: str) -> None:
    by_family = quality_of(_variant(HDR10_MP4, family, container=family)).score
    assert by_family == quality_of(_variant(HDR10_MP4, named, container=named)).score


@pytest.mark.parametrize(
    ("aac_channels", "ac3_channels", "aac_above"),
    [
        pytest.param(6, 2, True, id="aac_5.1_over_ac3_2.0"),
        pytest.param(3, 2, True, id="three_channels_are_multichannel"),
        pytest.param(2, 1, False, id="two_channels_are_stereo"),
    ],
)
def test_lossy_audio_above_two_channels_outranks_any_stereo_codec(
    aac_channels: int, ac3_channels: int, aac_above: bool
) -> None:
    aac, ac3 = _audio("aac", aac_channels), _audio("ac3", ac3_channels)
    assert (quality_of(aac).score > quality_of(ac3).score) is aac_above


def test_hdr_ranks_dolby_vision_then_hdr10_then_hlg_then_none() -> None:
    sdr = _copy("sdr", width=3840, height=2160, audio_codec="eac3")
    formats = (HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG, None)
    scores = [quality_of(_variant(sdr, f"sdr-{hdr}", hdr_format=hdr)).score for hdr in formats]
    assert [higher > lower for higher, lower in pairwise(scores)] == [True, True, True]


def test_audio_ranks_by_tier_then_codec_then_channels_and_dts_as_lossy() -> None:
    tiers = [
        {quality_of(_audio(codec, channels)).score for codec, channels in tier}
        for tier in _AUDIO_LADDER
    ]
    assert [len(tier) for tier in tiers] == [1] * len(_AUDIO_LADDER), "each row scores alike"
    heads = [min(tier) for tier in tiers]
    assert [higher > lower for higher, lower in pairwise(heads)] == [True] * (len(heads) - 1)


def test_a_retracted_copy_is_no_way_at_all() -> None:
    living_room = _living_room()
    assert len(rank((living_room,), (HDR10_MP4,))) == 2, "the premise: available, it has ways"
    assert rank((living_room,), (_variant(HDR10_MP4, "retracted", available=False),)) == ()


def test_a_series_shaped_row_with_no_facts_is_no_way_at_all() -> None:
    """A series' own row names no container, so no source can stream it."""
    living_room = _living_room()
    series = _copy("series")
    facts = ("container", "video_codec", "audio_codec", "width", "height", "hdr_format")
    assert [getattr(series, fact) for fact in (*facts, "audio_channels")] == [None] * 7, (
        "the premise: the row carries no fact at all"
    )
    assert series.available, "the premise: it is not skipped as retracted"
    assert len(rank((living_room,), (HDR10_MP4,))) == 2, "the premise: a copy with one has ways"
    assert rank((living_room,), (series,)) == ()


def test_a_way_whose_copy_names_no_container_gives_up_no_container() -> None:
    """`rank` makes no way of such a copy; `gives_up` alone still names no unknown a loss."""
    lesser = _variant(FULL, "lesser", container="mp4")
    _tall_enough("cast.shield", FULL, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, FULL))
    assert other.gives_up == ("NO MKV",), "the premise: a known lesser container is a loss"
    unnamed = replace(other, quality=replace(other.quality, container=None))
    assert gives_up(unnamed, best) == ()


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


def test_gives_up_names_every_known_shortfall_against_the_best() -> None:
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
    assert gives_up(other, best) == ("1080P", "NO HDR10", "NO TRUEHD", "NO MKV")
    assert other.gives_up == ("1080P", "NO HDR10", "NO TRUEHD", "NO MKV")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param({"width": 1920, "height": 1080}, ("1080P",), id="lower_resolution"),
        pytest.param({"width": None, "height": None}, (), id="resolution_unknown"),
        pytest.param({"hdr_format": None}, ("NO HDR10",), id="sdr"),
        pytest.param(
            {"video_codec": None, "width": None, "height": None, "hdr_format": None},
            (),
            id="video_unknown",
        ),
        pytest.param({"audio_codec": "eac3"}, ("NO TRUEHD",), id="lossy_audio"),
        pytest.param({"audio_codec": None}, (), id="audio_codec_unknown"),
        pytest.param({"audio_channels": 6}, ("5.1",), id="six_channels"),
        pytest.param({"audio_channels": 4}, ("4CH",), id="four_channels"),
        pytest.param({"audio_channels": 2}, ("STEREO",), id="two_channels"),
        pytest.param({"audio_channels": 1}, ("MONO",), id="one_channel"),
        pytest.param({"audio_channels": None}, (), id="channels_unknown"),
        pytest.param({"audio_channels": 0}, (), id="zero_channels"),
        pytest.param({"container": "mp4"}, ("NO MKV",), id="lesser_container"),
    ],
)
def test_a_way_gives_up_only_what_its_copy_is_known_to_lack(
    changes: dict[str, Any], expected: tuple[str, ...]
) -> None:
    lesser = _variant(FULL, "lesser", **changes)
    _tall_enough("cast.shield", FULL, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, FULL))
    assert (best.file.external_id, other.file.external_id) == ("full", "lesser"), (
        "the premise: every fact known ranks first"
    )
    assert other.gives_up == expected


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"width": None, "height": None, "hdr_format": None}, id="codec_only"),
        pytest.param({"video_codec": None, "height": None, "hdr_format": None}, id="width_only"),
        pytest.param({"video_codec": None, "width": None, "hdr_format": None}, id="height_only"),
        pytest.param(
            {"video_codec": None, "width": None, "height": None, "hdr_format": HdrFormat.HLG},
            id="hdr_only",
        ),
    ],
)
def test_any_one_video_fact_makes_a_missing_hdr_a_known_loss(changes: dict[str, Any]) -> None:
    """Over Infuse, which admits HLG and a copy with no video codec."""
    lesser = _variant(FULL, "lesser", **changes)
    _tall_enough("apple_tv.infuse", FULL, lesser)
    best, other, *_refused = rank((_living_room(),), (lesser, FULL))
    assert (best.file.external_id, other.file.external_id, other.playable) == (
        "full",
        "lesser",
        True,
    ), "the premise: every fact known ranks first, and the lesser copy plays"
    assert other.gives_up == ("NO HDR10",)


@pytest.mark.parametrize(
    ("channels", "word"),
    [
        pytest.param(6, "5.1", id="six"),
        pytest.param(7, "6.1", id="seven"),
        pytest.param(8, "7.1", id="eight"),
        pytest.param(9, "9CH", id="nine"),
    ],
)
def test_a_channel_loss_names_the_layout_a_person_knows(channels: int, word: str) -> None:
    better = _variant(FULL, "better", audio_channels=12)
    lesser = _variant(FULL, "lesser", audio_channels=channels)
    _tall_enough("cast.shield", better, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, better))
    assert (best.file.external_id, other.file.external_id) == ("better", "lesser"), (
        "the premise: more channels rank first"
    )
    assert other.gives_up == (word,)


def test_every_audio_rank_fits_inside_one_tier() -> None:
    """The audio score packs a tier and a codec rank into one number: no rank may leave a tier."""
    spellings = ("pcm_s16le", "pcm_s24le", "PCM_F32LE")
    assert {_audio_rank(_codec(name)) for name in spellings} == {_AUDIO_RANK["pcm"]}, (
        "the premise: every PCM spelling reads PCM's rank from the table"
    )
    assert {_audio_rank(_codec(name)) for name in (None, "vorbis")} == {_UNKNOWN_AUDIO_RANK}, (
        "the premise: a missing or unlisted codec takes the named default"
    )
    ranks = {**_AUDIO_RANK, "unlisted": _UNKNOWN_AUDIO_RANK}
    assert {codec: rank for codec, rank in ranks.items() if not 0 <= rank < _TIER_SPAN} == {}


@pytest.mark.parametrize(
    ("best_audio", "way_audio", "expected"),
    [
        pytest.param(("aac", 6), ("ac3", 2), ("STEREO",), id="a_better_codec_in_stereo"),
        pytest.param(("eac3", 6), ("ac3", 6), ("NO EAC3",), id="a_lesser_codec"),
        pytest.param(("eac3", 6), ("aac", 2), ("NO EAC3", "STEREO"), id="both"),
        pytest.param(("flac", 2), ("dts", 6), ("NO FLAC",), id="dts_is_lossy"),
        pytest.param(("pcm_s24le", 2), ("eac3", 2), ("NO PCM",), id="pcm_by_its_family"),
    ],
)
def test_an_audio_loss_names_the_codec_and_the_channels_apart(
    best_audio: tuple[str, int], way_audio: tuple[str, int], expected: tuple[str, ...]
) -> None:
    (best_codec, best_channels), (way_codec, way_channels) = best_audio, way_audio
    better = _variant(FULL, "better", audio_codec=best_codec, audio_channels=best_channels)
    lesser = _variant(FULL, "lesser", audio_codec=way_codec, audio_channels=way_channels)
    _tall_enough("cast.shield", better, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, better))
    assert (best.file.external_id, other.file.external_id) == ("better", "lesser"), (
        "the premise: the ranking chose the better audio"
    )
    assert other.gives_up == expected


def test_a_container_loss_names_the_family_of_the_best_copy() -> None:
    cast = RECIPE["cast.shield"]
    loose = replace(cast, key="test.loose", direct_play=replace(cast.direct_play, containers=None))
    den = Screen(
        area_id="den",
        name="Den",
        devices=(_shield("media_player.shield", "SHIELD", "den", (loose,)),),
    )
    webm = _variant(HDR10_MP4, "webm", container="webm")
    _tall_enough("cast.shield", HDR10_MP4, webm)
    best, other = rank((den,), (webm, HDR10_MP4))
    assert (best.file.external_id, other.playable) == ("hdr10-mp4", True), (
        "the premise: the MP4 is the best and the WebM plays"
    )
    assert other.gives_up == ("NO MP4",)


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
        ("1080P", "NO DOLBY VISION", "NO TRUEHD", "5.1"),
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
    assert [way.gives_up for way in ways] == [
        (),
        ("NO HDR10",),
        ("NEEDS A PRESS",),
        ("NO HDR10", "NEEDS A PRESS"),
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
