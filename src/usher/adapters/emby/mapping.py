# src/usher/adapters/emby/mapping.py
"""Emby's JSON, translated into `usher.ports.source`'s DTOs.

Pure functions, no HTTP: everything here is tested against the committed
fixtures with no server of any kind, which is what makes those fixtures a
real drift guard. If the fake server and this module got a field name wrong
in the same way, the contract suite would still pass and
`tests/unit/test_adapters_emby_mapping.py` would not.

**No Emby field name appears anywhere else in `src/`.** This module is the
whole of the translation, which is how PRD 01's "raw Emby or TMDb JSON
never escapes its adapter package" is enforced rather than merely stated.

### On the fixtures

`tests/fixtures/emby/*.json` are shape-recorded and value-synthetic: field
names, nesting, and types transcribed from real Emby 4.9.5.0 responses;
every value invented. A real capture is not committed, for three separate
reasons -- it embeds TMDb-sourced metadata that TMDb's terms forbid
redistributing (and CLAUDE.md's "ship importers, never data" already
forbids committing), it identifies a real library, and it carries real
server and user ids. `scripts/capture_emby_fixture.py` regenerates a
scrubbed capture locally for anyone who wants to diff shapes.

### Three traps this module exists to close

1. **Dolby Vision reports itself several ways at once**, and a DV stream
   commonly *also* advertises HDR10, because the HDR10 base layer is
   genuinely present. Checking `VideoRangeType` first would catalogue every
   DV file as HDR10, so any DV marker wins.
2. **Naive datetimes.** Verified on Python 3.13: `fromisoformat` accepts
   Emby's seven-digit fractional seconds and a trailing `Z`, but a value
   with no offset yields a naive datetime -- and `SourceItem` is a plain
   dataclass, so nothing catches it until a `TIMESTAMPTZ` insert much
   later. Emby's timestamps are UTC, so the offset is attached.
3. **The first audio stream is not the default one.** Commentary tracks
   are routinely index 0. `IsDefault` decides.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState

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
    "DOVI": HdrFormat.DOLBY_VISION,
    "DOLBYVISION": HdrFormat.DOLBY_VISION,
    "DV": HdrFormat.DOLBY_VISION,
    "HDR10": HdrFormat.HDR10,
    "HDR10PLUS": HdrFormat.HDR10,
    "HDR": HdrFormat.HDR10,
    "HLG": HdrFormat.HLG,
}

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


def _as_int(value: object) -> int | None:
    # `bool` is an `int` subclass, and Emby's JSON is full of booleans in
    # fields adjacent to numeric ones -- without this guard `Played: true`
    # in the wrong slot would become the integer 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _lower(value: object) -> str | None:
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
    """Format a `since` cursor for Emby's date query parameters.

    Normalised to UTC and **widened by one second**, deliberately. The port
    promises `since` is inclusive; whether Emby's own comparison is `>=` or
    `>` is not verified against the live server. One second earlier is
    correct under either -- an inclusive server returns a superset, which
    the port explicitly permits because callers deduplicate by
    `external_id`; an exclusive one still returns the boundary item. The
    opposite mistake, assuming inclusivity and being wrong, silently drops
    exactly the item the previous walk's cursor was set from, once per
    walk, forever.
    """
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


def primary_media_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first `MediaSources` entry, or `None` for a folder item."""
    sources = payload.get("MediaSources")
    if not isinstance(sources, list):
        return None
    for source in sources:
        if isinstance(source, Mapping):
            return source
    return None


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


def hdr_format(video: Mapping[str, Any]) -> HdrFormat | None:
    """The canonical `HdrFormat` for a video stream, or `None` for SDR.

    Any Dolby Vision marker wins outright -- see the module docstring.
    """
    profile = str(video.get("Profile") or "").lower()
    if video.get("DvProfile") is not None or "dolby vision" in profile or "dvhe" in profile:
        return HdrFormat.DOLBY_VISION
    for key in ("VideoRangeType", "VideoRange"):
        mapped = _HDR_BY_TOKEN.get(_NON_ALNUM.sub("", str(video.get(key) or "")).upper())
        if mapped is not None:
            return mapped
    return None


def audio_token(audio: Mapping[str, Any]) -> str | None:
    """A single lowercase token describing an audio stream as a client
    thinks about it: `truehd_atmos_7_1`, `eac3_5_1`, `aac_2_0`.

    This is `StreamTarget.audio`, and it is a different thing from
    `SourceItem.audio_codec`'s raw `"truehd"` -- the codec alone does not
    tell a client whether it can play the track, which is the whole point
    of PRD 07 returning ranked targets rather than one URL. An unknown
    channel count falls back to `{n}ch` rather than being dropped, so a
    9.1.6 track is still described rather than silently reported as
    channel-less.
    """
    codec = _lower(audio.get("Codec"))
    if codec is None:
        return None
    parts = [codec]
    descriptor = f"{audio.get('Profile') or ''} {audio.get('Title') or ''}".lower()
    for needle, token in _AUDIO_FEATURES:
        if needle in descriptor:
            parts.append(token)
            break
    channels = _as_int(audio.get("Channels"))
    if channels is not None and channels > 0:
        parts.append(_CHANNEL_LAYOUTS.get(channels, f"{channels}ch"))
    return "_".join(parts)


def to_source_item(payload: Mapping[str, Any]) -> SourceItem | None:
    """One Emby item into a `SourceItem`.

    `None` for an item type Usher does not model -- Season, BoxSet,
    Playlist, Folder. `list_items` asks for only the three types below, but
    a server that ignores `IncludeItemTypes` must not abort a 94,395-item
    walk over a box set. An item with no `Id` is different: it cannot be
    upserted on `(source_id, external_id)` at all, so skipping it would
    lose a real item with no trace, and it raises `PortDataMalformed`.
    """
    external_id = _text(payload.get("Id"))
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
    runtime_ticks = _as_int(payload.get("RunTimeTicks"))
    return SourceItem(
        external_id=external_id,
        name=_text(payload.get("Name")) or external_id,
        kind=kind,
        year=_as_int(payload.get("ProductionYear")),
        provider_ids=provider_ids(payload.get("ProviderIds")),
        container=_lower(media_source.get("Container")),
        video_codec=_lower(video.get("Codec")),
        audio_codec=_lower(audio.get("Codec")),
        # Item-level Width/Height are the fallback: Emby sets them on the
        # item for some libraries and only on the video stream for others.
        width=_as_int(video.get("Width")) or _as_int(payload.get("Width")),
        height=_as_int(video.get("Height")) or _as_int(payload.get("Height")),
        hdr_format=hdr_format(video),
        audio_channels=_as_int(audio.get("Channels")),
        file_size_bytes=_as_int(media_source.get("Size")),
        runtime_seconds=None if runtime_ticks is None else runtime_ticks // TICKS_PER_SECOND,
        added_at=parse_datetime(payload.get("DateCreated")),
        series_external_id=_text(payload.get("SeriesId")),
        season_number=_as_int(payload.get("ParentIndexNumber")),
        episode_number=_as_int(payload.get("IndexNumber")),
        # PRD 03 stores this verbatim in `raw_payloads`. Copied rather than
        # aliased so a caller that mutates the DTO cannot reach back into
        # whatever buffer the response was parsed from.
        raw=dict(payload),
    )


def to_watch_state(
    payload: Mapping[str, Any], *, source_user_id: str | None
) -> SourceWatchState | None:
    """One Emby item's `UserData` into a `SourceWatchState`.

    `None` when the item carries no `UserData` at all, which means the
    field was not requested or this item type has none. That is a different
    claim from a zero state: emitting zeros here would push "unwatched"
    over whatever Usher already knows. A `UserData` block that *is* present
    and happens to be all zeros is emitted -- see the port's `watch_state`
    docstring for why filtering those is a correctness bug.
    """
    external_id = _text(payload.get("Id"))
    user_data = payload.get("UserData")
    if external_id is None or not isinstance(user_data, Mapping):
        return None
    ticks = _as_int(user_data.get("PlaybackPositionTicks")) or 0
    return SourceWatchState(
        external_id=external_id,
        position_seconds=max(ticks, 0) // TICKS_PER_SECOND,
        played=bool(user_data.get("Played", False)),
        play_count=max(_as_int(user_data.get("PlayCount")) or 0, 0),
        last_played_at=parse_datetime(user_data.get("LastPlayedDate")),
        source_user_id=source_user_id,
    )
