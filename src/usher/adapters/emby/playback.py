# src/usher/adapters/emby/playback.py
"""`StreamTarget`s for one Emby item.

PRD 07: Usher "supplies complete information and never proxies bytes", and
"the deep-link construction currently done by hand in the Home Assistant
card moves here, where it is testable". This module is that move, and it is
a pure function of one item payload so that it stays testable.

Two targets per playable item, ranked:

1. **direct** -- `/Videos/{id}/stream.{container}?static=true`, the
   byte-for-byte file, carrying every fact a client needs to decide whether
   it can play it. **Verified to actually serve bytes** against the live
   Emby 4.9.5.0 server on 2026-07-31: a range request against a URL this
   module built answered 206 with `video/x-matroska` content.
2. **deep_link** -- `infuse://x-callback-url/play?url=<the direct URL,
   percent-encoded>`, built by `usher.ports.source.wrap_deep_link`. The
   wrapper itself does not live here (M9, D2): a custom scheme is not
   something a playback ticket's HTTP redirect can produce, so whatever
   mints the ticket has to be able to call it too, and `usher.services`/
   `usher.api` may not import this module (import contract 6). See that
   function's docstring for the full reasoning; this module only calls it.

Direct first, because a client that *can* play the container should: a deep
link hands playback to another application, which is a fallback rather than
a preference. The deep link deliberately carries no quality facts -- the
client is not choosing a stream there, it is delegating, and duplicating
the facts would invite a UI to render them twice.

`/Items/{id}/PlaybackInfo` is deliberately not called. That endpoint exists
for transcode negotiation, which Usher explicitly does not do, and
everything the direct URL needs -- container, `MediaSourceId`, resume
position -- is already on the item. One fewer endpoint to have guessed
wrong, and one fewer round trip against an upstream whose single-item reads
are measured at **0.1495 s median / 0.1649 s mean** (M10 S1, 2026-08-15 --
`.claude/rules/emby-push-and-ingest.md`), on the request path a person is
waiting on.

**The direct URL carries the source's access token**, because without it
the bytes are not fetchable and Usher does not proxy them -- measured, not
assumed: the same URL with `api_key` removed answers **401**, and with
`static` removed answers **400**. That is knowingly in tension with PRD
08's "no credential ever reaches a client"; ADR-0012
(`docs/prd/decisions/`) records the decision, how it differs from the
failure Usher replaces, and what removes it in M9. The handling rule that
follows -- the token is a return value and never a log field -- is enforced
by `StreamTarget`'s own `__repr__`, not by this module remembering: see
`usher.ports.source`.

**It does not carry `DeviceId`, and used to.** Removing it from the query
leaves the route answering 206 with real bytes, so it was never
load-bearing here. Sending it made a captured playback URL a drop-in for
the push channel's own `/embywebsocket?api_key=…&deviceId=…` parameters and
attributed anything done with it to Usher's registered device -- a risk
ADR-0012 accepted only because nobody had checked whether the parameter was
needed.
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

    # Three parameters, not four. Measured against the live Emby 4.9.5.0
    # server on 2026-07-31, one request each with a `Range` header: the URL
    # as built answers 206 with real bytes; with `DeviceId` removed it still
    # answers 206; with `api_key` removed it answers 401; with `static`
    # removed it answers 400. `DeviceId` was never load-bearing here, and
    # sending it made a captured URL a drop-in for the push channel's
    # `/embywebsocket?api_key=…&deviceId=…` -- a risk ADR-0012 accepted only
    # because nobody had checked.
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
