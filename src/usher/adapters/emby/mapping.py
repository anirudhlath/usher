# src/usher/adapters/emby/mapping.py
"""Emby's JSON, translated into `usher.ports.source`'s DTOs."""

import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import MAXYEAR, UTC, datetime, timedelta
from functools import partial
from typing import Any

from loguru import logger
from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import INT32_MAX, SourceItem, SourceItemKind, SourceWatchState

# Emby counts in 100-nanosecond ticks, everywhere: runtimes, playback
# positions, durations.
TICKS_PER_SECOND = 10_000_000

_ITEM_KINDS: dict[str, SourceItemKind] = {
    "Movie": SourceItemKind.MOVIE,
    "Series": SourceItemKind.SERIES,
    "Episode": SourceItemKind.EPISODE,
}

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

# HDR10Plus deliberately maps to HDR10: `HdrFormat` has no HDR10+ member,
# and HDR10+ genuinely carries an HDR10 base layer, so this is lossy rather
# than wrong -- and far better than reporting the file as SDR.
_HDR_BY_TOKEN: dict[str, HdrFormat] = {
    "DV": HdrFormat.DOLBY_VISION,
    "HDR10": HdrFormat.HDR10,
    "HDR10PLUS": HdrFormat.HDR10,
    "HDR": HdrFormat.HDR10,
    "HLG": HdrFormat.HLG,
}

# Matched as prefixes, and checked before the exact table above, because Emby's
# `VideoRangeType` names the *base layer* alongside the DV marker: `DOVIWithHDR10`,
# `DOVIWithHLG`, `DOVIWithSDR`, `DOVIWithEL`.
_DV_TOKEN_PREFIXES = ("DOVI", "DOLBYVISION")

# Every field that can name a video range, most specific first.
_RANGE_KEYS = ("ExtendedVideoType", "ExtendedVideoSubType", "VideoRangeType", "VideoRange")

# Ordered: the first match wins, so "DTS-HD MA" is not also matched by a
# looser "master audio" rule further down producing a different token.
_AUDIO_FEATURES: tuple[tuple[str, str], ...] = (
    ("atmos", "atmos"),
    ("dts:x", "x"),
    ("dts-x", "x"),
    ("dts-hd ma", "hd_ma"),
    ("master audio", "hd_ma"),
)

_CHANNEL_LAYOUTS: dict[int, str] = {1: "1_0", 2: "2_0", 6: "5_1", 8: "7_1"}

# `file_size_bytes` is the one `bigint`; every other integer the port carries lands in
# an `integer` column (`INT32_MAX`).
_INT64_MAX = 2**63 - 1


def _stored(number: int | None, high: int, *, field: str, item: str) -> int | None:
    """`number` if it is within `[0, high]`, else `None`, logged.

    Every column these land in is CHECKed non-negative, and a value past the type
    fails its whole batch in asyncpg's encoder -- an `OverflowError`, not a
    `UsherPortError`, so the walk aborts. No real value reaches either end, so one
    outside is corrupt; the log line is what says how often that happens.
    """
    if number is None or 0 <= number <= high:
        return number
    logger.warning(
        "Emby item {item!r}: {field} {number} cannot be stored; recorded as unknown",
        item=item,
        field=field,
        number=number,
    )
    return None


def _label(payload: Mapping[str, Any]) -> str:
    """The item's name, truncated: enough to find it in the Emby UI."""
    return str(as_text(payload.get("Name")) or as_text(payload.get("Id")) or "<unnamed>")[:60]


def as_int(value: object) -> int | None:
    # `bool` is an `int` subclass, and Emby's JSON is full of booleans in
    # fields adjacent to numeric ones -- without this guard `Played: true`
    # in the wrong slot would become the integer 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # `json.loads` accepts `NaN` and `Infinity`, and `int()` raises on both.
        return int(value) if math.isfinite(value) else None
    return None


def as_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def as_lower(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def parse_datetime(value: object) -> datetime | None:
    """Emby's ISO 8601 into an aware datetime, or `None` if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def emby_datetime(value: datetime) -> str:
    """Format a `since` cursor for Emby's date query parameters."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "a `since` cursor must be timezone-aware; a naive one shifts the whole "
            "delta window by the host's UTC offset and silently drops the difference"
        )
    return (value.astimezone(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def provider_ids(raw: object) -> dict[str, str]:
    """Emby's `ProviderIds` into the port's lowercase canonical keys."""
    if not isinstance(raw, Mapping):
        return {}
    return {
        key.lower(): value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def stream_of(media_source: Mapping[str, Any], stream_type: str) -> Mapping[str, Any] | None:
    """The default stream of `stream_type`, falling back to the first.

    `IsDefault` rather than index 0: commentary tracks are routinely the
    first audio stream, and reporting a commentary's codec and channel
    layout as the item's is both wrong and the kind of wrong nobody
    notices.
    """
    streams = media_source.get("MediaStreams")
    if not isinstance(streams, list):
        return None
    candidates = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("Type") == stream_type
    ]
    if not candidates:
        return None
    for stream in candidates:
        if stream.get("IsDefault"):
            return stream
    return candidates[0]


def _playback_rank(media_source: Mapping[str, Any]) -> tuple[int, int]:
    """How good a version is: pixels first, bytes as the tiebreak.

    Bytes second rather than first because a bigger file is not a better
    one -- a bloated 1080p remux outweighs an efficient 4K encode -- but
    between two versions of the same resolution it is the only signal on
    the payload that distinguishes them at all.
    """
    video = stream_of(media_source, "Video") or {}
    width = as_int(video.get("Width")) or 0
    height = as_int(video.get("Height")) or 0
    return width * height, as_int(media_source.get("Size")) or 0


def primary_media_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The version Usher describes and plays, or `None` for a folder item."""
    sources = payload.get("MediaSources")
    if not isinstance(sources, list):
        return None
    candidates = [source for source in sources if isinstance(source, Mapping)]
    if not candidates:
        return None
    playable = [source for source in candidates if as_lower(source.get("Container"))]
    return max(playable or candidates, key=_playback_rank)


def runtime_seconds(payload: Mapping[str, Any], media_source: Mapping[str, Any]) -> int | None:
    """An item's runtime in whole seconds, or `None` if it has none.

    Item level first, the chosen version's own `RunTimeTicks` second -- Emby
    emits it in both places and not always in both at once.

    Called by `to_source_item` *and* by `build_stream_targets`, so the catalog
    and the playback target cannot describe one file's runtime differently.
    """
    ticks = as_int(payload.get("RunTimeTicks"))
    if ticks is None:
        ticks = as_int(media_source.get("RunTimeTicks"))
    if ticks is None:
        return None
    return _stored(
        ticks // TICKS_PER_SECOND, INT32_MAX, field="runtime_seconds", item=_label(payload)
    )


def position_seconds(ticks: int, *, item: str) -> int | None:
    """A resume position in whole seconds: negative is `0`, too large to store `None`.

    Floor division rounds towards negative infinity, hence the clamp first.
    """
    return _stored(max(ticks, 0) // TICKS_PER_SECOND, INT32_MAX, field="position", item=item)


def hdr_format(video: Mapping[str, Any]) -> HdrFormat | None:
    """The canonical `HdrFormat` for a video stream, or `None` for SDR.

    Any Dolby Vision marker wins outright.
    """
    profile = str(video.get("Profile") or "").lower()
    tokens = [_NON_ALNUM.sub("", str(video.get(key) or "")).upper() for key in _RANGE_KEYS]
    if (
        video.get("DvProfile") is not None
        or "dolby vision" in profile
        or "dvhe" in profile
        or any(token.startswith(_DV_TOKEN_PREFIXES) for token in tokens)
    ):
        return HdrFormat.DOLBY_VISION
    for token in tokens:
        mapped = _HDR_BY_TOKEN.get(token)
        if mapped is not None:
            return mapped
    return None


def audio_token(audio: Mapping[str, Any]) -> str | None:
    """A single lowercase token describing an audio stream as a client thinks about it.

    `truehd_atmos_7_1`, `eac3_5_1`, `aac_2_0`.

    This is `StreamTarget.audio`, a different thing from
    `SourceItem.audio_codec`'s raw `"truehd"`: the codec alone does not tell a
    client whether it can play the track. An unknown channel count falls back to
    `{n}ch` rather than being dropped, so a 9.1.6 track is still described.
    """
    codec = as_lower(audio.get("Codec"))
    if codec is None:
        return None
    parts = [codec]
    descriptor = f"{audio.get('Profile') or ''} {audio.get('Title') or ''}".lower()
    for needle, token in _AUDIO_FEATURES:
        if needle in descriptor:
            parts.append(token)
            break
    channels = as_int(audio.get("Channels"))
    if channels is not None and channels > 0:
        parts.append(_CHANNEL_LAYOUTS.get(channels, f"{channels}ch"))
    return "_".join(parts)


def to_source_item(payload: Mapping[str, Any]) -> SourceItem | None:
    """One Emby item into a `SourceItem`.

    `None` for an item type Usher does not model -- Season, BoxSet, Playlist,
    Folder. `list_items` asks for only the three types below, but a server that
    ignores `IncludeItemTypes` must not abort a whole-library walk over a box
    set. An item with no `Id` is different: it cannot be upserted on
    `(source_id, external_id)` at all, so skipping it would lose a real item
    with no trace, and it raises `PortDataMalformed`.
    """
    external_id = as_text(payload.get("Id"))
    if external_id is None:
        raise PortDataMalformed(
            "Emby item has no Id",
            # The name, truncated -- enough to find the item in the Emby UI,
            # short enough not to be a payload dump.
            detail=str(payload.get("Name", "<unnamed>"))[:60],
        )
    kind = _ITEM_KINDS.get(str(payload.get("Type") or ""))
    if kind is None:
        return None
    media_source = primary_media_source(payload) or {}
    video = stream_of(media_source, "Video") or {}
    audio = stream_of(media_source, "Audio") or {}
    stored = partial(_stored, item=_label(payload))
    return SourceItem(
        external_id=external_id,
        name=as_text(payload.get("Name")) or external_id,
        kind=kind,
        # Narrower than the column: the name+year probe computes `year + 1`.
        year=stored(as_int(payload.get("ProductionYear")), MAXYEAR, field="year"),
        provider_ids=provider_ids(payload.get("ProviderIds")),
        container=as_lower(media_source.get("Container")),
        video_codec=as_lower(video.get("Codec")),
        audio_codec=as_lower(audio.get("Codec")),
        # Item-level Width/Height are the fallback: Emby sets them on the
        # item for some libraries and only on the video stream for others.
        width=stored(
            as_int(video.get("Width")) or as_int(payload.get("Width")), INT32_MAX, field="width"
        ),
        height=stored(
            as_int(video.get("Height")) or as_int(payload.get("Height")), INT32_MAX, field="height"
        ),
        hdr_format=hdr_format(video),
        audio_channels=stored(as_int(audio.get("Channels")), INT32_MAX, field="audio_channels"),
        file_size_bytes=stored(
            as_int(media_source.get("Size")), _INT64_MAX, field="file_size_bytes"
        ),
        runtime_seconds=runtime_seconds(payload, media_source),
        added_at=parse_datetime(payload.get("DateCreated")),
        series_external_id=as_text(payload.get("SeriesId")),
        season_number=stored(
            as_int(payload.get("ParentIndexNumber")), INT32_MAX, field="season_number"
        ),
        episode_number=stored(
            as_int(payload.get("IndexNumber")), INT32_MAX, field="episode_number"
        ),
        # Opaque above the adapter, and never stored: PRD 03 stores no source payload.
        raw=deepcopy(dict(payload)),
    )


def to_watch_state(
    payload: Mapping[str, Any],
    *,
    source_user_id: str | None,
    play_history_is_trustworthy: bool,
) -> SourceWatchState | None:
    """One Emby item's `UserData` into a `SourceWatchState`.

    `None` for a position too large to store: `position_seconds` is not optional,
    and a fabricated `0` would overwrite a real resume point.
    """
    external_id = as_text(payload.get("Id"))
    user_data = payload.get("UserData")
    if external_id is None or not isinstance(user_data, Mapping):
        return None
    ticks = as_int(user_data.get("PlaybackPositionTicks")) or 0
    position = position_seconds(ticks, item=external_id)
    if position is None:
        return None
    play_count: int | None = None
    last_played_at: AwareDatetime | None = None
    if play_history_is_trustworthy:
        counted = as_int(user_data.get("PlayCount"))
        play_count = (
            None
            if counted is None
            else _stored(max(counted, 0), INT32_MAX, field="play_count", item=external_id)
        )
        last_played_at = parse_datetime(user_data.get("LastPlayedDate"))
    return SourceWatchState(
        external_id=external_id,
        position_seconds=position,
        played=bool(user_data.get("Played", False)),
        play_count=play_count,
        last_played_at=last_played_at,
        source_user_id=source_user_id,
    )


def user_data_states(
    entries: Sequence[Any], *, source_user_id: str | None
) -> tuple[list[str], list[SourceWatchState]]:
    """A `UserDataChanged` message's `UserDataList` into ids and states."""
    ids: list[str] = []
    states: list[SourceWatchState] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        external_id = as_text(entry.get("ItemId"))
        if external_id is None:
            continue
        ticks = as_int(entry.get("PlaybackPositionTicks")) or 0
        position = position_seconds(ticks, item=external_id)
        # Dropped from both lists, which the event pairs up.
        if position is None:
            continue
        ids.append(external_id)
        states.append(
            SourceWatchState(
                external_id=external_id,
                position_seconds=position,
                played=bool(entry.get("Played", False)),
                play_count=None,
                last_played_at=None,
                source_user_id=source_user_id,
            )
        )
    return ids, states


def library_ids(raw: object) -> tuple[str, ...]:
    """One `LibraryChanged` array into external ids.

    Non-string and empty entries are dropped rather than raising: this
    message arrives on a long-lived channel with no job behind it, so a
    malformed entry that took the lane down would cost a reconnect and a
    gap-closing delta walk, where dropping it costs one id the nightly
    reconcile picks up anyway.
    """
    if not isinstance(raw, list):
        return ()
    return tuple(text for value in raw if (text := as_text(value)) is not None)
