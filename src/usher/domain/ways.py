"""Every way a title's copies could play on the house's screens, ranked.

Pure: a screen, a recipe and a copy in; a way, or a refusal saying why, out.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Final

from usher.domain.enums import HdrFormat
from usher.domain.recipes import Recipe
from usher.domain.screens import Screen, ScreenDevice, reading_key
from usher.domain.source import MediaItem


class Resolution(IntEnum):
    UNKNOWN = 0
    SD = 1
    HD_720 = 2
    HD_1080 = 3
    UHD_2160 = 4


RESOLUTION_LABELS: Final[dict[Resolution, str]] = {
    Resolution.UNKNOWN: "UNKNOWN RESOLUTION",
    Resolution.SD: "SD",
    Resolution.HD_720: "720P",
    Resolution.HD_1080: "1080P",
    Resolution.UHD_2160: "4K",
}
HDR_LABELS: Final[dict[HdrFormat, str]] = {
    HdrFormat.DOLBY_VISION: "DOLBY VISION",
    HdrFormat.HDR10: "HDR10",
    HdrFormat.HLG: "HLG",
}
_HDR_RANK: Final[dict[HdrFormat, int]] = {
    HdrFormat.DOLBY_VISION: 3,
    HdrFormat.HDR10: 2,
    HdrFormat.HLG: 1,
}
# Lossless object audio, lossless, lossy multichannel, stereo-class lossy.
_AUDIO_RANK: Final[dict[str, int]] = {
    "truehd": 5,
    "dts": 4,
    "flac": 4,
    "eac3": 3,
    "ac3": 2,
    "aac": 1,
    "opus": 1,
    "mp3": 1,
}
_CONTAINER_RANK: Final[dict[str, int]] = {"mkv": 2, "mp4": 1, "m4v": 1, "mov": 1}
_WAY_NAMESPACE: Final = uuid.UUID("5d2c7a8e-3f41-4b9e-9a57-0c6e1d2f4b13")


def resolution_of(width: int | None, height: int | None) -> Resolution:
    """A copy's class by either dimension, so a letterboxed 3840x1600 is still 4K."""
    wide, tall = width or 0, height or 0
    if wide >= 3200 or tall >= 1800:
        return Resolution.UHD_2160
    if wide >= 1800 or tall >= 900:
        return Resolution.HD_1080
    if wide >= 1200 or tall >= 650:
        return Resolution.HD_720
    return Resolution.SD if wide or tall else Resolution.UNKNOWN


def _parts(container: str | None) -> tuple[str, ...]:
    """`mov,mp4,m4a,3gp` is one container under several names, and each one counts."""
    return tuple(part.strip().lower() for part in (container or "").split(",") if part.strip())


def _hdr_rank(hdr: HdrFormat | None) -> int:
    return _HDR_RANK[hdr] if hdr is not None else 0


def _audio_rank(codec: str | None) -> int:
    name = (codec or "").lower()
    return 4 if name.startswith("pcm") else _AUDIO_RANK.get(name, 0)


@dataclass(frozen=True, slots=True)
class Quality:
    resolution: Resolution
    hdr: HdrFormat | None
    audio_codec: str | None
    audio_channels: int | None
    container: str | None

    @property
    def score(self) -> tuple[int, int, int, int, int]:
        """Resolution, then HDR, then audio, then its channels, then the container."""
        container = max(
            (_CONTAINER_RANK.get(part, 0) for part in _parts(self.container)), default=0
        )
        return (
            int(self.resolution),
            _hdr_rank(self.hdr),
            _audio_rank(self.audio_codec),
            self.audio_channels or 0,
            container,
        )


def quality_of(item: MediaItem) -> Quality:
    return Quality(
        resolution=resolution_of(item.width, item.height),
        hdr=item.hdr_format,
        audio_codec=item.audio_codec,
        audio_channels=item.audio_channels,
        container=item.container,
    )


def refusal(item: MediaItem, recipe: Recipe) -> str | None:
    """Why this copy cannot direct-play through this recipe, in words, or `None`.

    Conservative only about what is known: a fact the copy does not carry is
    never a reason, and `facts_known` says when the answer rests on less.
    """
    profile, via = recipe.direct_play, recipe.name.upper()
    parts = _parts(item.container)
    if profile.containers is not None and parts and not set(parts) & profile.containers:
        return f"{parts[0].upper()} OVER {via}"
    codec = (item.video_codec or "").lower()
    if profile.video_codecs is not None and codec and codec not in profile.video_codecs:
        return f"{codec.upper()} OVER {via}"
    if item.hdr_format is not None and item.hdr_format not in profile.hdr_formats:
        return f"NO {HDR_LABELS[item.hdr_format]} OVER {via}"
    audio = (item.audio_codec or "").lower()
    if profile.audio_codecs is not None and audio and audio not in profile.audio_codecs:
        return f"{audio.upper()} OVER {via}"
    if item.height is not None and item.height > profile.max_height:
        return f"{item.height}P OVER {via}"
    return None


def facts_known(item: MediaItem, recipe: Recipe) -> bool:
    """Whether the copy carries every fact the recipe's profile constrains."""
    profile = recipe.direct_play
    return (
        (profile.containers is None or bool(_parts(item.container)))
        and (profile.video_codecs is None or bool(item.video_codec))
        and (profile.audio_codecs is None or bool(item.audio_codec))
        and item.height is not None
    )


@dataclass(frozen=True, slots=True)
class Way:
    """One copy, on one player, through one recipe: playable, or refused with its reason."""

    id: uuid.UUID
    screen: Screen
    device: ScreenDevice
    recipe: Recipe
    file: MediaItem
    quality: Quality
    refusal: str | None
    facts_known: bool
    best: bool = False
    gives_up: tuple[str, ...] = ()

    @property
    def playable(self) -> bool:
        return self.refusal is None


def way_id(area_id: str, entity_id: str, recipe_key: str, media_item_id: uuid.UUID) -> uuid.UUID:
    """Stable over (screen, player, recipe, copy), so a client can send the way it showed."""
    return uuid.uuid5(
        _WAY_NAMESPACE, "|".join((area_id, entity_id, recipe_key, str(media_item_id)))
    )


def gives_up(way: Way, best: Way) -> tuple[str, ...]:
    """What `way` loses against `best`, in the words a screen prints."""
    words: list[str] = []
    if way.quality.resolution < best.quality.resolution:
        words.append(RESOLUTION_LABELS[way.quality.resolution])
    if best.quality.hdr is not None and _hdr_rank(way.quality.hdr) < _hdr_rank(best.quality.hdr):
        words.append(f"NO {HDR_LABELS[best.quality.hdr]}")
    best_audio = best.quality.audio_codec
    if best_audio and _audio_rank(way.quality.audio_codec) < _audio_rank(best_audio):
        words.append(f"NO {best_audio.upper()}")
    if best.recipe.hands_free and not way.recipe.hands_free:
        words.append("NEEDS A PRESS")
    return tuple(words)


def _way(screen: Screen, device: ScreenDevice, recipe: Recipe, item: MediaItem) -> Way:
    return Way(
        id=way_id(screen.area_id, device.entity_id, recipe.key, item.id),
        screen=screen,
        device=device,
        recipe=recipe,
        file=item,
        quality=quality_of(item),
        refusal=refusal(item, recipe),
        facts_known=facts_known(item, recipe),
    )


def _order(way: Way) -> tuple[int, tuple[int, ...], bool, str, str, str, str]:
    tier = 0 if way.playable and way.recipe.hands_free else 1 if way.playable else 2
    return (
        tier,
        tuple(-part for part in way.quality.score),
        not way.recipe.proven,
        *reading_key(way.device.name, way.device.entity_id),
        way.recipe.key,
        str(way.file.id),
    )


def _against(way: Way, best: Way | None) -> Way:
    if best is None or not way.playable:
        return way
    if way is best:
        return replace(way, best=True)
    return replace(way, gives_up=gives_up(way, best))


def rank(screens: Sequence[Screen], files: Sequence[MediaItem]) -> tuple[Way, ...]:
    """Every copy, player and matching recipe, grouped by screen and ordered within it.

    Within a screen: hands-free ways by score, then ways that need a press by
    score, then the ways that cannot play; an equal score puts a proven recipe
    first. A copy its source has retracted is no way at all. The first playable
    way of each screen is its best, and every other playable way says what it
    gives up against that one.
    """
    ranked: list[Way] = []
    for screen in sorted(screens, key=lambda one: reading_key(one.name, one.area_id)):
        ways = sorted(
            (
                _way(screen, device, recipe, item)
                for device in screen.devices
                for recipe in device.recipes
                for item in files
                if item.available
            ),
            key=_order,
        )
        best = next((way for way in ways if way.playable), None)
        ranked.extend(_against(way, best) for way in ways)
    return tuple(ranked)
