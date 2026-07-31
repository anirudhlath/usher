# src/usher/adapters/emby/mapping.py
"""Emby's JSON, translated into `usher.ports.source`'s DTOs.

Pure functions, no HTTP: everything here is tested against the committed
fixtures with no server of any kind, which is what makes those fixtures a
real drift guard. If the fake server and this module got a field name wrong
in the same way, the contract suite would still pass and
`tests/unit/test_adapters_emby_mapping.py` would not.

**No Emby field name appears outside `usher.adapters.emby`.** That package
reads Emby's JSON in exactly two modules -- this one, and `playback`, which
builds direct-play URLs out of the same item payload -- and `playback`
coerces its values with `as_int`/`as_text`/`as_lower` from here rather than
redefining them, so "parse an Emby integer" has one meaning in the package
and cannot drift between an item's `SourceItem` and its `StreamTarget`.
Nothing above the adapter boundary names an Emby field at all, which is how
PRD 01's "raw Emby or TMDb JSON never escapes its adapter package" is
enforced rather than merely stated.

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
   DV file as HDR10, so any DV marker wins -- `DvProfile`, a `dvhe`/`Dolby
   Vision` codec profile, or a `VideoRangeType` naming DV. That last one is
   matched by *prefix*, because Emby spells the base layer into the same
   token (`DOVIWithHDR10`, `DOVIWithHLG`, `DOVIWithSDR`, `DOVIWithEL`) and
   an exact-match table would send every one of those to its base layer
   instead. Each marker is independently sufficient and independently
   tested: a file carrying only `DvProfile` is DV, and so is one carrying
   only `DOVIWithHDR10`.
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
from copy import deepcopy
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
    "DV": HdrFormat.DOLBY_VISION,
    "HDR10": HdrFormat.HDR10,
    "HDR10PLUS": HdrFormat.HDR10,
    "HDR": HdrFormat.HDR10,
    "HLG": HdrFormat.HLG,
}

# Matched as prefixes, and checked before the exact table above, because
# Emby's `VideoRangeType` names the *base layer* alongside the DV marker:
# `DOVIWithHDR10`, `DOVIWithHLG`, `DOVIWithSDR`, `DOVIWithEL`. An
# exact-match table gets `DOVI` right and every compound spelling wrong,
# and gets it wrong *quietly* -- `DOVIWithHDR10` falls through to
# `VideoRange: "HDR"` and is catalogued as HDR10, `DOVIWithSDR` as SDR.
# A prefix rule also covers a combination Emby adds after this was
# written, which an enumerated table by construction cannot.
_DV_TOKEN_PREFIXES = ("DOVI", "DOLBYVISION")

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


def as_int(value: object) -> int | None:
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
    """Format a `since` cursor for Emby's date query parameters.

    Normalised to UTC and **widened by one to two seconds**, deliberately.
    The port promises `since` is inclusive; whether Emby's own comparison
    is `>=` or `>` is not verified against the live server. One second
    earlier is correct under either -- an inclusive server returns a
    superset, which the port explicitly permits because callers deduplicate
    by `external_id`; an exclusive one still returns the boundary item. The
    opposite mistake, assuming inclusivity and being wrong, silently drops
    exactly the item the previous walk's cursor was set from, once per
    walk, forever.

    "One to two", not "one": the format Emby's date parameters take carries
    whole seconds only, so a cursor of `12:00:00.9` is widened by the
    explicit second *and* by the 0.9 that truncating discards. Both errors
    point the same way -- wider -- so this is stated rather than corrected.
    Rounding instead would make the total exactly one second on average and
    sometimes *less* than one, which is the direction that loses items.

    **A naive `value` raises.** `AwareDatetime` is a bare annotation on
    `list_items(since=...)`, which pydantic never validates -- it is a
    plain method, not a model field -- so nothing but this stops a naive
    datetime arriving. `astimezone` then interprets it in whatever zone the
    *host* is in: measured on a UTC-5 machine, a naive `12:00` became
    `MinDateLastSaved=2026-07-20T16:59:59Z`, skipping five hours of
    changes. That is the direction the widening above exists to avoid, at
    eighteen thousand times its size, and it reports nothing -- the walk
    just quietly returns fewer items than it should, every time. Refused
    rather than assumed-UTC: a caller that meant UTC can say so.
    """
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
    """The version Usher describes and plays, or `None` for a folder item.

    **Not `MediaSources[0]`.** Emby lists one entry per *version*: the same
    film held at 4K and at 1080p is two entries, and a version Emby can
    only transcode is an entry with no `Container` at all. Taking the first
    entry takes whichever the server happened to list first, and both
    orderings occur:

    - 1080p listed before 4K makes the 4K version unreachable -- PRD 07's
      `/play` hands back a `1920x1080` target for a library holding both;
    - a transcode-only entry listed first has no container, and the
      container *is* the direct URL's file extension, so
      `build_stream_targets` returned `[]` -- "not playable here" for an
      item that plays fine.

    So: the highest-resolution entry that has a container, falling back to
    the first entry of any kind when none does (a genuinely transcode-only
    item still has a real codec, size and runtime to catalogue, even though
    there is no direct URL to build for it). `max` returns the first
    maximal element, so a single-source item and a set of equally-ranked
    versions both still resolve to `MediaSources[0]`.

    Both `to_source_item` and `build_stream_targets` call this, so an
    item's catalogued facts and its playback URL always describe the same
    file. Choosing separately is how a `/play` response comes to advertise
    one version's codecs and stream another's bytes.
    """
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

    Item level first, the chosen version's own `RunTimeTicks` second --
    Emby emits it in both places and not always in both at once.

    Called by `to_source_item` *and* by `build_stream_targets`, which is
    what makes this module's "cannot drift" claim true of derivation and
    not only of coercion. It previously held for `as_int` alone: the item
    field was read on one side and the media-source fallback existed only
    on the other, so an item carrying its runtime only on the media source
    was catalogued as `None` and played back as `9360` -- same payload,
    same call.
    """
    ticks = as_int(payload.get("RunTimeTicks"))
    if ticks is None:
        ticks = as_int(media_source.get("RunTimeTicks"))
    return None if ticks is None else ticks // TICKS_PER_SECOND


def hdr_format(video: Mapping[str, Any]) -> HdrFormat | None:
    """The canonical `HdrFormat` for a video stream, or `None` for SDR.

    Any Dolby Vision marker wins outright -- see the module docstring.
    """
    profile = str(video.get("Profile") or "").lower()
    if video.get("DvProfile") is not None or "dolby vision" in profile or "dvhe" in profile:
        return HdrFormat.DOLBY_VISION
    for key in ("VideoRangeType", "VideoRange"):
        token = _NON_ALNUM.sub("", str(video.get(key) or "")).upper()
        if token.startswith(_DV_TOKEN_PREFIXES):
            return HdrFormat.DOLBY_VISION
        mapped = _HDR_BY_TOKEN.get(token)
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

    `None` for an item type Usher does not model -- Season, BoxSet,
    Playlist, Folder. `list_items` asks for only the three types below, but
    a server that ignores `IncludeItemTypes` must not abort a 94,395-item
    walk over a box set. An item with no `Id` is different: it cannot be
    upserted on `(source_id, external_id)` at all, so skipping it would
    lose a real item with no trace, and it raises `PortDataMalformed`.
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
    return SourceItem(
        external_id=external_id,
        name=as_text(payload.get("Name")) or external_id,
        kind=kind,
        year=as_int(payload.get("ProductionYear")),
        provider_ids=provider_ids(payload.get("ProviderIds")),
        container=as_lower(media_source.get("Container")),
        video_codec=as_lower(video.get("Codec")),
        audio_codec=as_lower(audio.get("Codec")),
        # Item-level Width/Height are the fallback: Emby sets them on the
        # item for some libraries and only on the video stream for others.
        width=as_int(video.get("Width")) or as_int(payload.get("Width")),
        height=as_int(video.get("Height")) or as_int(payload.get("Height")),
        hdr_format=hdr_format(video),
        audio_channels=as_int(audio.get("Channels")),
        file_size_bytes=as_int(media_source.get("Size")),
        runtime_seconds=runtime_seconds(payload, media_source),
        added_at=parse_datetime(payload.get("DateCreated")),
        series_external_id=as_text(payload.get("SeriesId")),
        season_number=as_int(payload.get("ParentIndexNumber")),
        episode_number=as_int(payload.get("IndexNumber")),
        # PRD 03 stores this verbatim in `raw_payloads`. Deep-copied rather
        # than aliased so a caller that mutates the DTO cannot reach back
        # into whatever buffer the response was parsed from -- and
        # deep-copied rather than `dict(payload)`, because a shallow copy
        # leaves `raw["UserData"]` and every `raw["MediaSources"]` entry
        # pointing at the very objects `get_item` is still reading to build
        # the item's `StreamTarget`s. Measured at 17 us per item against
        # the movie fixture, i.e. ~1.6 s across the 94,395-item library
        # this was built for, against an upstream PRD 01 measures at 1-5 s
        # per *request*.
        raw=deepcopy(dict(payload)),
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
    external_id = as_text(payload.get("Id"))
    user_data = payload.get("UserData")
    if external_id is None or not isinstance(user_data, Mapping):
        return None
    ticks = as_int(user_data.get("PlaybackPositionTicks")) or 0
    return SourceWatchState(
        external_id=external_id,
        position_seconds=max(ticks, 0) // TICKS_PER_SECOND,
        played=bool(user_data.get("Played", False)),
        play_count=max(as_int(user_data.get("PlayCount")) or 0, 0),
        last_played_at=parse_datetime(user_data.get("LastPlayedDate")),
        source_user_id=source_user_id,
    )
