"""Response shape for `GET /titles/{id}/ways` and `GET /episodes/{id}/ways`."""

import uuid
from typing import Self

from pydantic import BaseModel

from usher.api.dto.screens import RecipeResponse
from usher.domain.enums import HdrFormat
from usher.domain.ways import RESOLUTION_LABELS, Way
from usher.services.ways import WaysAnswer

__all__ = ["ScreenWaysResponse", "WayFileResponse", "WayResponse", "WaysResponse"]


class WayFileResponse(BaseModel):
    """The copy a way plays, as the facts its ranking read. No `external_id`, no path."""

    source_id: uuid.UUID
    resolution: str
    container: str | None
    video_codec: str | None
    hdr_format: HdrFormat | None
    audio_codec: str | None
    audio_channels: int | None
    runtime_seconds: int | None


class WayResponse(BaseModel):
    """One copy on one player through one recipe.

    `refusal` is null exactly when `playable` is true. `gives_up` is empty on
    the best way and on every way that cannot play.
    """

    id: uuid.UUID
    entity_id: str
    device: str
    recipe: RecipeResponse
    file: WayFileResponse
    playable: bool
    refusal: str | None
    best: bool
    gives_up: list[str]
    facts_known: bool

    @classmethod
    def of(cls, way: Way) -> Self:
        item = way.file
        return cls(
            id=way.id,
            entity_id=way.device.entity_id,
            device=way.device.name,
            recipe=RecipeResponse.of(way.recipe),
            file=WayFileResponse(
                source_id=item.source_id,
                resolution=RESOLUTION_LABELS[way.quality.resolution],
                container=item.container,
                video_codec=item.video_codec,
                hdr_format=item.hdr_format,
                audio_codec=item.audio_codec,
                audio_channels=item.audio_channels,
                runtime_seconds=item.runtime_seconds,
            ),
            playable=way.playable,
            refusal=way.refusal,
            best=way.best,
            gives_up=list(way.gives_up),
            facts_known=way.facts_known,
        )


class ScreenWaysResponse(BaseModel):
    area_id: str
    name: str
    ways: list[WayResponse]


class WaysResponse(BaseModel):
    """Ways grouped by screen, in the ranking's order, each screen's best first."""

    screens_available: bool
    screens: list[ScreenWaysResponse]

    @classmethod
    def of(cls, answer: WaysAnswer) -> Self:
        grouped: dict[str, ScreenWaysResponse] = {}
        for way in answer.ways:
            screen = grouped.setdefault(
                way.screen.area_id,
                ScreenWaysResponse(area_id=way.screen.area_id, name=way.screen.name, ways=[]),
            )
            screen.ways.append(WayResponse.of(way))
        return cls(screens_available=answer.screens_available, screens=list(grouped.values()))
