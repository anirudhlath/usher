"""PRD 07's four watch actions: the request body, and the state they answer with."""

from pydantic import BaseModel, Field

from usher.api.dto.title import WatchStateResponse
from usher.domain.watch import WatchState

__all__ = ["WatchWriteRequest", "watch_state_response"]


class WatchWriteRequest(BaseModel):
    """`PUT /watch/titles/{id}` and `PUT /watch/episodes/{id}`.

    Both fields are required. There is no "leave the other one alone"
    spelling, and adding one would make a partial write reachable -- which is
    the defect M3 measured at the source rather than a hypothetical: Emby's
    `UserData` body deserialises into a DTO whose unset fields take their
    defaults, so a body carrying only `PlaybackPositionTicks` flips a played
    item to unplayed. A client that wants to change one of the two sends both,
    and the two `/played` routes exist precisely so the common one-field press
    needs no body at all.

    `position_seconds` is `ge=0` because the column and `WatchState` both are;
    without it a negative value reaches Postgres and comes back as a
    `CheckViolation` in a 500 rather than as a 422 naming the field.
    """

    position_seconds: int = Field(ge=0)
    played: bool


def watch_state_response(state: WatchState) -> WatchStateResponse:
    """The stored row as the wire shape.

    Built from the row `set_from_client` returned rather than from a re-read:
    a second `SELECT` is a second statement that can disagree, and on Postgres
    it would carry the trigger-stamped `updated_at` of a later instant.
    """
    return WatchStateResponse(
        position_seconds=state.position_seconds,
        played=state.played,
        play_count=state.play_count,
        last_played_at=state.last_played_at,
    )
