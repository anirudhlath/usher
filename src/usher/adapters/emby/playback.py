# src/usher/adapters/emby/playback.py
"""`StreamTarget`s for one Emby item."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    as_int,
    as_lower,
    as_text,
    audio_token,
    hdr_format,
    primary_media_source,
    runtime_seconds,
    stream_of,
)
from usher.ports.source import INFUSE_SCHEME, StreamTarget, StreamTargetKind, wrap_deep_link


def build_stream_targets(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    access_token: str,
) -> list[StreamTarget]:
    """Ranked ways to play one Emby item, or `[]` if there are none.

    Empty for a folder item (a series or season, which has no
    `MediaSources`) and for a media source with no container -- the
    container *is* the URL's file extension, and guessing one would hand a
    client a link that fails at play time. The port documents `[]` as the
    answer for "no way to play this", so neither case is an error.
    """
    external_id = as_text(payload.get("Id"))
    media_source = primary_media_source(payload)
    if media_source is None or external_id is None:
        return []
    container = as_lower(media_source.get("Container"))
    if container is None:
        return []

    video = stream_of(media_source, "Video") or {}
    audio = stream_of(media_source, "Audio") or {}
    width = as_int(video.get("Width")) or as_int(payload.get("Width"))
    height = as_int(video.get("Height")) or as_int(payload.get("Height"))
    user_data = payload.get("UserData")
    position_ticks = (
        as_int(user_data.get("PlaybackPositionTicks")) if isinstance(user_data, Mapping) else None
    )

    # Three parameters, not four.
    query = urlencode(
        {
            "static": "true",
            "MediaSourceId": as_text(media_source.get("Id")) or external_id,
            "api_key": access_token,
        }
    )
    url = f"{base_url.rstrip('/')}/Videos/{quote(external_id, safe='')}/stream.{container}?{query}"
    return [
        StreamTarget(
            kind=StreamTargetKind.DIRECT,
            url=url,
            container=container,
            video_codec=as_lower(video.get("Codec")),
            audio=audio_token(audio),
            hdr_format=hdr_format(video),
            resolution=(f"{width}x{height}" if width is not None and height is not None else None),
            # The same derivation `to_source_item` uses, not a second copy
            # of it: these two fields describe one file and must not be
            # able to disagree about it.
            runtime_seconds=runtime_seconds(payload, media_source),
            resume_position_seconds=(
                None if position_ticks is None else max(position_ticks, 0) // TICKS_PER_SECOND
            ),
        ),
        StreamTarget(
            kind=StreamTargetKind.DEEP_LINK,
            url=wrap_deep_link(url),
            scheme=INFUSE_SCHEME,
        ),
    ]
