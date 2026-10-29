"""Recipes: how an app on a device model is handed a URL, and what it can direct-play.

Keyed by HA's own identifiers; every fact starts assumed until proven at the TV.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from usher.domain.enums import HdrFormat


class Handover(StrEnum):
    """How the URL reaches the app. Slice 2's dispatcher acts on it."""

    INFUSE_CALLBACK = "infuse_callback"
    AIRPLAY_URL = "airplay_url"
    CAST_URL = "cast_url"


@dataclass(frozen=True, slots=True)
class DirectPlay:
    """What plays without transcoding. `None` constrains nothing about that fact.

    Every entry is lowercase and folded as a copy's fact is folded before the
    check, or it never matches: a container by the name its family folds to
    (`mkv`, never `matroska`), a video codec in lowercase (`hevc`, never
    `HEVC`), and PCM audio as `pcm`, never a sample format such as `pcm_s16le`.
    """

    containers: frozenset[str] | None
    video_codecs: frozenset[str] | None
    hdr_formats: frozenset[HdrFormat]
    audio_codecs: frozenset[str] | None
    max_height: int


@dataclass(frozen=True, slots=True)
class Recipe:
    """One way to play on one family of device.

    `proven` is false until a send at the TV confirms the profile, the handover
    and the hands-free claim; a proven recipe ranks above an assumed one of
    equal quality. `name` is the app as a screen names it.
    """

    key: str
    name: str
    integration: str
    model_prefixes: tuple[str, ...]
    app_id: str | None
    handover: Handover
    direct_play: DirectPlay
    hands_free: bool
    proven: bool

    def matches(self, integration: str, model: str | None) -> bool:
        """By integration *and* model prefix, so a Cast speaker is not a SHIELD."""
        return (
            integration == self.integration
            and model is not None
            and model.startswith(self.model_prefixes)
        )


_HEVC_AND_H264: Final = frozenset({"hevc", "h265", "h264", "avc"})

RECIPES: Final[tuple[Recipe, ...]] = (
    Recipe(
        key="apple_tv.infuse",
        name="Infuse",
        integration="apple_tv",
        model_prefixes=("Apple TV 4K",),
        app_id="com.firecore.infuse",
        handover=Handover.INFUSE_CALLBACK,
        direct_play=DirectPlay(
            containers=frozenset({"mkv", "mp4", "m4v", "mov"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG}),
            audio_codecs=None,
            max_height=2160,
        ),
        # Spike 1 at the TV: Infuse opened and played with no prompt and no press.
        hands_free=True,
        proven=False,
    ),
    Recipe(
        key="apple_tv.airplay",
        name="AirPlay",
        integration="apple_tv",
        model_prefixes=("Apple TV 4K",),
        app_id=None,
        handover=Handover.AIRPLAY_URL,
        direct_play=DirectPlay(
            containers=frozenset({"mp4", "m4v", "mov"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.DOLBY_VISION, HdrFormat.HDR10}),
            audio_codecs=frozenset({"aac", "ac3", "eac3"}),
            max_height=2160,
        ),
        hands_free=True,
        proven=False,
    ),
    Recipe(
        key="cast.shield",
        name="Cast",
        integration="cast",
        model_prefixes=("SHIELD",),
        app_id=None,
        handover=Handover.CAST_URL,
        direct_play=DirectPlay(
            containers=frozenset({"mkv", "mp4", "m4v"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.HDR10}),
            audio_codecs=None,
            max_height=2160,
        ),
        hands_free=True,
        proven=False,
    ),
)


def recipes_for(
    integration: str, model: str | None, recipes: tuple[Recipe, ...] = RECIPES
) -> tuple[Recipe, ...]:
    return tuple(recipe for recipe in recipes if recipe.matches(integration, model))
