# src/usher/adapters/emby/playback.py
"""`StreamTarget`s for one Emby item.

PRD 07: Usher "supplies complete information and never proxies bytes", and
"the deep-link construction currently done by hand in the Home Assistant
card moves here, where it is testable". This module is that move, and it is
a pure function of one item payload so that it stays testable.

Two targets per playable item, ranked:

1. **direct** -- `/Videos/{id}/stream.{container}?static=true`, the
   byte-for-byte file, carrying every fact a client needs to decide whether
   it can play it.
2. **deep_link** -- `infuse://x-callback-url/play?url=<the direct URL,
   percent-encoded>`.

Direct first, because a client that *can* play the container should: a deep
link hands playback to another application, which is a fallback rather than
a preference. The deep link deliberately carries no quality facts -- the
client is not choosing a stream there, it is delegating, and duplicating
the facts would invite a UI to render them twice.

`/Items/{id}/PlaybackInfo` is deliberately not called. That endpoint exists
for transcode negotiation, which Usher explicitly does not do, and
everything the direct URL needs -- container, `MediaSourceId`, resume
position -- is already on the item. One fewer endpoint to have guessed
wrong, and one fewer round trip against an upstream PRD 01 measures at
1-5 s per request.

**The direct URL carries the source's access token**, because without it
the bytes are not fetchable and Usher does not proxy them. That is
knowingly in tension with PRD 08's "no credential ever reaches a client";
ADR-0012 (`docs/prd/decisions/`)
records the decision, how it differs from the failure Usher replaces, and
what removes it in M9. The handling rule that follows -- the token is a
return value and never a log field -- is enforced by `StreamTarget`'s own
`__repr__`, not by this module remembering: see `usher.ports.source`.
"""

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
    stream_of,
)
from usher.ports.source import StreamTarget, StreamTargetKind

INFUSE_SCHEME = "infuse"


def build_stream_targets(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    access_token: str,
    device_id: str,
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
    runtime_ticks = as_int(payload.get("RunTimeTicks")) or as_int(media_source.get("RunTimeTicks"))
    user_data = payload.get("UserData")
    position_ticks = (
        as_int(user_data.get("PlaybackPositionTicks")) if isinstance(user_data, Mapping) else None
    )

    query = urlencode(
        {
            "static": "true",
            "MediaSourceId": as_text(media_source.get("Id")) or external_id,
            "DeviceId": device_id,
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
            runtime_seconds=(None if runtime_ticks is None else runtime_ticks // TICKS_PER_SECOND),
            resume_position_seconds=(
                None if position_ticks is None else max(position_ticks, 0) // TICKS_PER_SECOND
            ),
        ),
        StreamTarget(
            kind=StreamTargetKind.DEEP_LINK,
            url=f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(url, safe='')}",
            scheme=INFUSE_SCHEME,
        ),
    ]
