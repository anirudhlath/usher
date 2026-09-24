"""Response shape for `POST /titles/{id}/play` and `POST /episodes/{id}/play`."""

import uuid
from typing import Self

from pydantic import BaseModel

from usher.domain.enums import HdrFormat
from usher.ports.source import StreamTargetKind
from usher.services.playback import PlaybackResolution, PlaybackTarget

__all__ = ["PlayResponse", "PlaySourceResponse", "PlayTargetResponse"]


class PlaySourceResponse(BaseModel):
    """Which configured source serves this target.

    The operator's own name, which is what PRD 07's example shows and what a
    client renders in a picker. Nothing source-specific: no `base_url`, no
    `credentials_ref`, no `device_id`, no `external_id`.
    """

    id: uuid.UUID
    name: str


class PlayTargetResponse(BaseModel):
    """One ranked way to play, as a client sees it.

    Every field of `StreamTarget`, named one at a time rather than dumped.

    **`url` is a ticket URL and never a source URL.** It is an absolute
    `https://.../stream/{ticket}` for a `direct` target, or a deep link
    wrapping one for a `deep_link` target. Following it is a `302` to the real
    target; see `api/routers/playback.py`.
    """

    kind: StreamTargetKind
    url: str
    scheme: str | None = None
    container: str | None = None
    video_codec: str | None = None
    audio: str | None = None
    hdr_format: HdrFormat | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None
    source: PlaySourceResponse

    @classmethod
    def of(cls, resolved: PlaybackTarget) -> Self:
        """Field by field, deliberately."""
        target = resolved.target
        return cls(
            kind=target.kind,
            url=target.url,
            scheme=target.scheme,
            container=target.container,
            video_codec=target.video_codec,
            audio=target.audio,
            hdr_format=target.hdr_format,
            resolution=target.resolution,
            runtime_seconds=target.runtime_seconds,
            resume_position_seconds=target.resume_position_seconds,
            source=PlaySourceResponse(id=resolved.source_id, name=resolved.source_name),
        )


class PlayResponse(BaseModel):
    """The ranked targets, in the order the resolution produced them.

    A list and nothing else. There is no `count`, no `status` and no
    `detail`: this shape is only ever built for `PlaybackStatus.PLAYABLE`,
    whose `PlaybackResolution.__post_init__` refuses to exist with an empty
    `targets`, and the other two statuses are problem documents rather than
    a 200 with a flag in it.
    """

    targets: list[PlayTargetResponse]

    @classmethod
    def of(cls, resolution: PlaybackResolution) -> Self:
        return cls(targets=[PlayTargetResponse.of(one) for one in resolution.targets])
