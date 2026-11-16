"""One send to a screen: its stages, its record, and what a player's reading says of each."""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from usher.domain.house import HouseState
from usher.domain.recipes import Recipe


class SendStage(StrEnum):
    WAKING = "waking"
    OPENING = "opening"
    STARTING = "starting"
    PLAYING = "playing"
    DIDNT_TAKE = "didnt_take"
    SUPERSEDED = "superseded"


FINISHED: Final = frozenset({SendStage.PLAYING, SendStage.DIDNT_TAKE, SendStage.SUPERSEDED})

STAYED_ASLEEP: Final = "{device} STAYED ASLEEP"
MAY_NOT_BE_INSTALLED: Final = "{app} MAY NOT BE INSTALLED"
NOT_ANSWERING: Final = "HOME ASSISTANT IS NOT ANSWERING"
USHER_FAILED: Final = "USHER FAILED"

_SEPARATOR: Final = " · "
_OFF_STATES: Final = frozenset({"off", "standby", "unavailable"})
_RUNTIME_TOLERANCE_PERCENT: Final = 2


@dataclass(frozen=True, slots=True)
class StageMark:
    stage: SendStage
    at: datetime
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Send:
    """A send's record. `marks` is never empty; the last says where the send stands."""

    id: uuid.UUID
    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    way_id: uuid.UUID
    area_id: str
    screen_name: str
    entity_id: str
    device_name: str
    recipe_key: str
    marks: tuple[StageMark, ...]
    failed_stage: SendStage | None = None

    @property
    def stage(self) -> SendStage:
        return self.marks[-1].stage

    @property
    def detail(self) -> str | None:
        return self.marks[-1].detail

    @property
    def finished(self) -> bool:
        return self.stage in FINISHED

    def moved(
        self,
        stage: SendStage,
        at: datetime,
        *,
        detail: str | None = None,
        failed_stage: SendStage | None = None,
    ) -> "Send":
        """The send one mark on; a finished send does not move."""
        if self.finished:
            raise ValueError(f"a send that is {self.stage} does not move")
        if failed_stage is not None and stage is not SendStage.DIDNT_TAKE:
            raise ValueError("only a send that did not take has a failed stage")
        return replace(
            self,
            marks=(*self.marks, StageMark(stage=stage, at=at, detail=detail)),
            failed_stage=failed_stage,
        )


def busy_with(state: HouseState | None) -> str | None:
    """What a playing player is busy with; `None` when it is not playing."""
    if state is None or state.state != "playing":
        return None
    parts = [one.upper() for one in (state.app_name, state.media_title) if one]
    return _SEPARATOR.join(parts) if parts else "SOMETHING ELSE"


def opened(now: HouseState | None, *, at_handover: HouseState | None, app_id: str | None) -> bool:
    """The reading changed since the handover and, for a recipe with an app, shows that app."""
    if now is None or now == at_handover:
        return False
    return app_id is None or now.app_id == app_id


def started(
    now: HouseState | None,
    *,
    at_handover: HouseState | None,
    app_id: str | None,
    runtime_seconds: int | None,
) -> bool:
    """Opened, playing, and a duration within 2% of the runtime when both are known."""
    if now is None or not opened(now, at_handover=at_handover, app_id=app_id):
        return False
    if now.state != "playing":
        return False
    if now.media_duration_seconds is None or runtime_seconds is None:
        return True
    return abs(now.media_duration_seconds - runtime_seconds) * 100 <= (
        runtime_seconds * _RUNTIME_TOLERANCE_PERCENT
    )


def showing(state: HouseState | None, *, device_name: str, recipe: Recipe) -> str:
    """What the player shows at a deadline, as one printed line."""
    if state is None or state.state in _OFF_STATES:
        return f"{device_name.upper()} TURNED OFF"
    if recipe.app_id is not None and state.app_id != recipe.app_id and state.app_name:
        return f"SOMEONE OPENED {state.app_name.upper()}"
    return f"{recipe.name.upper()} DID NOT START PLAYING"


def didnt_take(stage: SendStage, cause: str) -> str:
    return _SEPARATOR.join(("DIDN'T TAKE", stage.value.upper(), cause))
