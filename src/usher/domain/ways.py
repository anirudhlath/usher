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
# A codec's rank within its audio tier.
_AUDIO_RANK: Final[dict[str, int]] = {
    "truehd": 5,
    "dts": 4,
    "flac": 4,
    "pcm": 4,
    "eac3": 3,
    "ac3": 2,
    "aac": 1,
    "opus": 1,
    "mp3": 1,
}
# The rank of a codec the table does not list, or of none at all.
_UNKNOWN_AUDIO_RANK: Final = 0
_LOSSLESS_OBJECT_AUDIO: Final = frozenset({"truehd"})
_LOSSLESS_AUDIO: Final = frozenset({"flac", "pcm"})
# Above every codec rank, so the audio score is the tier first and the codec within it.
_TIER_SPAN: Final = 10
_CHANNEL_WORDS: Final[dict[int, str]] = {1: "MONO", 2: "STEREO", 6: "5.1", 7: "6.1", 8: "7.1"}
_CONTAINER_RANK: Final[dict[str, int]] = {"mkv": 2, "mp4": 1, "m4v": 1, "mov": 1}
_CONTAINER_LABELS: Final[dict[int, str]] = {2: "MKV", 1: "MP4"}
# ffprobe's family name for Matroska; WebM keeps its own, since not every profile takes it.
_CONTAINER_ALIASES: Final[dict[str, str]] = {"matroska": "mkv"}
_WAY_NAMESPACE: Final = uuid.UUID("5d2c7a8e-3f41-4b9e-9a57-0c6e1d2f4b13")


class _AudioTier(IntEnum):
    NONE = 0
    STEREO = 1
    MULTICHANNEL = 2
    LOSSLESS = 3
    LOSSLESS_OBJECT = 4


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
    names = (part.strip().lower() for part in (container or "").split(","))
    return tuple(_CONTAINER_ALIASES.get(name, name) for name in names if name)


def _container_rank(container: str | None) -> int:
    return max((_CONTAINER_RANK.get(part, 0) for part in _parts(container)), default=0)


def _hdr_rank(hdr: HdrFormat | None) -> int:
    return _HDR_RANK[hdr] if hdr is not None else 0


def _codec(codec: str | None) -> str:
    """A codec's name in lowercase, every PCM sample format named `pcm`."""
    name = (codec or "").lower()
    return "pcm" if name.startswith("pcm") else name


def _audio_rank(name: str) -> int:
    return _AUDIO_RANK.get(name, _UNKNOWN_AUDIO_RANK)


def _lossless_tier(name: str) -> _AudioTier:
    if name in _LOSSLESS_OBJECT_AUDIO:
        return _AudioTier.LOSSLESS_OBJECT
    if name in _LOSSLESS_AUDIO:
        return _AudioTier.LOSSLESS
    return _AudioTier.NONE


def _audio_tier(codec: str | None, channels: int | None) -> _AudioTier:
    """Lossless by codec; lossy by channels. DTS is lossy: only its core is certain."""
    name = _codec(codec)
    if not name:
        return _AudioTier.NONE
    lossless = _lossless_tier(name)
    if lossless is not _AudioTier.NONE:
        return lossless
    return _AudioTier.MULTICHANNEL if (channels or 0) > 2 else _AudioTier.STEREO


def _audio_class(codec: str | None) -> tuple[int, int]:
    """What the codec alone is worth, its channels aside: lossless first, then its rank."""
    name = _codec(codec)
    return int(_lossless_tier(name)), _audio_rank(name)


def _video_known(item: MediaItem) -> bool:
    return bool(item.video_codec) or any(
        fact is not None for fact in (item.width, item.height, item.hdr_format)
    )


@dataclass(frozen=True, slots=True)
class Quality:
    resolution: Resolution
    hdr: HdrFormat | None
    audio_codec: str | None
    audio_channels: int | None
    container: str | None

    @property
    def score(self) -> tuple[int, int, int, int, int]:
        """Resolution, then HDR, then audio by tier and codec, then its channels, then container.

        The tiers: lossless object audio, lossless, lossy multichannel, then stereo.
        """
        tier = _audio_tier(self.audio_codec, self.audio_channels)
        return (
            int(self.resolution),
            _hdr_rank(self.hdr),
            tier * _TIER_SPAN + _audio_rank(_codec(self.audio_codec)),
            self.audio_channels or 0,
            _container_rank(self.container),
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
    audio = _codec(item.audio_codec)
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
    """What `way` is known to lose against `best`, in the words a screen prints.

    A fact its copy does not carry, zero channels included, is never named a
    loss; `facts_known` carries that doubt. The audio codec and its channels
    are named apart, so a better codec in stereo says `STEREO` and nothing
    about the codec.
    """
    mine, theirs = way.quality, best.quality
    words: list[str] = []
    if mine.resolution is not Resolution.UNKNOWN and mine.resolution < theirs.resolution:
        words.append(RESOLUTION_LABELS[mine.resolution])
    hdr_lost = _hdr_rank(mine.hdr) < _hdr_rank(theirs.hdr)
    if theirs.hdr is not None and hdr_lost and _video_known(way.file):
        words.append(f"NO {HDR_LABELS[theirs.hdr]}")
    if mine.audio_codec and _audio_class(mine.audio_codec) < _audio_class(theirs.audio_codec):
        words.append(f"NO {_codec(theirs.audio_codec).upper()}")
    channels = mine.audio_channels
    if channels and channels < (theirs.audio_channels or 0):
        words.append(_CHANNEL_WORDS.get(channels, f"{channels}CH"))
    container = _container_rank(theirs.container)
    if _parts(mine.container) and _container_rank(mine.container) < container:
        words.append(f"NO {_CONTAINER_LABELS[container]}")
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
