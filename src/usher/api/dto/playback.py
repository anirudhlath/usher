"""Response shape for `POST /titles/{id}/play` and `POST /episodes/{id}/play`.

**Every field is named, and that is a security control rather than a style.**
Nothing in this module calls `dataclasses.asdict`, `astuple`, `vars`,
`__dict__`, `json.dumps` over a port DTO, or a pydantic `TypeAdapter` dump of
`StreamTarget`. ADR-0012 (`docs/prd/decisions/0012-playback-urls-carry-a-
source-token.md`) measured all six of those returning
`StreamTarget.url` **in full** -- they are
field-access paths, and `StreamTarget.__repr__`'s redaction closes none of
them. The service one layer down has already substituted a ticket for every
url (`usher.services.playback`), so a bulk dump would publish a ticket rather
than a token today; it would publish the token the day a target reaches here
unsubstituted, and a bulk dump is precisely the spelling that would not notice.
Naming ten fields is the cost of the failure being a `TypeError` instead.

**`source` is per target, not per response**, which is where the shipped shape
diverges from PRD 07's original example. `PlaybackTarget` carries the
attribution because a household with two copies of one film has two sources
and one list of targets ranked across both -- a response-level `source` object
could only be right for a household with one. The PRD's `## Playback` section
carries the corrected example.

**Every model here ends in `Response`, including the nested ones.** That is the
convention the whole of `api/dto/` keeps (`WatchStateResponse`,
`AvailabilityResponse`, `RowCardResponse` are all nested), and it is
load-bearing rather than cosmetic: `tests/unit/test_api_dto.py` discovers
response models by `name.endswith("Response")` and asserts none of them
declares a credential-shaped field or a `SecretStr`. `PlayTargetResponse` is
the model in this package that renders a value derived from a
credential-bearing URL, so it is exactly the model that scan should cover --
the same argument `ProblemResponse`'s own docstring makes for its name. The D4
plan spelled these `PlayTarget`/`PlaySource`; under those names they would be
the only models in `api/dto/` the scan cannot see.
"""

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

    The ten fields of `StreamTarget`, named one at a time -- see the module
    docstring for why a dump is not an option here.

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
        """Field by field, deliberately. See the module docstring."""
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
