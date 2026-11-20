"""Shapes for the three send routes."""

import uuid
from typing import Self

from pydantic import AwareDatetime, BaseModel

from usher.domain.sends import Send, SendStage

__all__ = ["SendRequest", "SendResponse", "SendStageResponse"]


class SendRequest(BaseModel):
    """The way to play, and whether to play over what a screen is already playing."""

    way: uuid.UUID
    take_over: bool = False


class SendStageResponse(BaseModel):
    """One stage a send entered, and when."""

    stage: SendStage
    at: AwareDatetime
    detail: str | None


class SendResponse(BaseModel):
    """A send and every stage it has entered, in order.

    `failed_stage` is set exactly when `stage` is `didnt_take`, and `detail`
    then says what the player showed. No field carries a URL.
    """

    send_id: uuid.UUID
    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    way_id: uuid.UUID
    screen: str
    screen_name: str
    entity_id: str
    recipe: str
    stage: SendStage
    failed_stage: SendStage | None
    detail: str | None
    stages: list[SendStageResponse]

    @classmethod
    def of(cls, send: Send) -> Self:
        return cls(
            send_id=send.id,
            title_id=send.title_id,
            episode_id=send.episode_id,
            way_id=send.way_id,
            screen=send.area_id,
            screen_name=send.screen_name,
            entity_id=send.entity_id,
            recipe=send.recipe_key,
            stage=send.stage,
            failed_stage=send.failed_stage,
            detail=send.detail,
            stages=[
                SendStageResponse(stage=mark.stage, at=mark.at, detail=mark.detail)
                for mark in send.marks
            ],
        )
