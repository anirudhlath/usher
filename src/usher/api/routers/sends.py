"""Send a copy to a screen, and follow the send."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import (
    DefaultUserIdDep,
    EpisodeRepositoryDep,
    SendDispatcherDep,
    SendServiceDep,
    TitleRepositoryDep,
)
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.sends import SendRequest, SendResponse
from usher.api.errors import ProblemException
from usher.api.routers.screens import players_unavailable
from usher.services.screens import PLAYERS_OFF, PlayersUnavailable
from usher.services.send import (
    ScreenBusy,
    SourceUnavailable,
    UnknownWay,
    WayNotPlayable,
)

router = APIRouter(tags=["screens"])

_NO_SUCH_WAY: Final = "no way has this id"

_SEND_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such title or episode."},
    409: {
        "model": ProblemResponse,
        "description": (
            "`way_not_playable`: the way cannot play this copy, or its recipe is not proven. "
            "`screen_busy`: the player is playing something and the send did not ask to take "
            "over; `detail` says what."
        ),
    },
    422: {
        "model": ProblemResponse,
        "description": "The request was rejected, or `way` names no way of this item.",
    },
    503: {
        "model": ProblemResponse,
        "description": (
            "`players_unavailable`: players are off, or Home Assistant cannot be read; "
            "`Retry-After` is present when asking again may succeed. "
            "`source_unavailable`: the source holding the copy could not be reached."
        ),
    },
}

_READ_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ProblemResponse,
        "description": (
            "No such send: unknown, dropped from the last 32 finished, or sent before a restart."
        ),
    },
    422: {"model": ProblemResponse, "description": "The request was rejected."},
    503: {"model": ProblemResponse, "description": "Players are off."},
}


@router.post(
    "/titles/{title_id}/send",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SendResponse,
    responses=_SEND_FAILURES,
    summary="Play a title on a screen, the way chosen",
)
async def send_title(
    title_id: uuid.UUID,
    body: SendRequest,
    titles: TitleRepositoryDep,
    sends: SendServiceDep,
    user_id: DefaultUserIdDep,
) -> SendResponse:
    """Start a send and answer at once; its stages follow on `/events` and at `GET /sends/{id}`.

    Every refusal comes before anything is written to the player or minted.
    """
    title = await titles.get(title_id)
    if title is None:
        raise _not_found("title not found")
    try:
        sent = await sends.send_title(
            title, way_id=body.way, take_over=body.take_over, user_id=user_id
        )
    except (UnknownWay, WayNotPlayable, ScreenBusy, SourceUnavailable, PlayersUnavailable) as error:
        raise _refusal(error) from None
    return SendResponse.of(sent)


@router.post(
    "/episodes/{episode_id}/send",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SendResponse,
    responses=_SEND_FAILURES,
    summary="Play an episode on a screen, the way chosen",
)
async def send_episode(
    episode_id: uuid.UUID,
    body: SendRequest,
    episodes: EpisodeRepositoryDep,
    titles: TitleRepositoryDep,
    sends: SendServiceDep,
    user_id: DefaultUserIdDep,
) -> SendResponse:
    """The same for one episode, resuming at the default user's place in it."""
    episode = (await episodes.list_by_ids([episode_id])).get(episode_id)
    if episode is None:
        raise _not_found("episode not found")
    try:
        sent = await sends.send_episode(
            await titles.get(episode.title_id),
            episode,
            way_id=body.way,
            take_over=body.take_over,
            user_id=user_id,
        )
    except (UnknownWay, WayNotPlayable, ScreenBusy, SourceUnavailable, PlayersUnavailable) as error:
        raise _refusal(error) from None
    return SendResponse.of(sent)


@router.get(
    "/sends/{send_id}",
    response_model=SendResponse,
    responses=_READ_FAILURES,
    summary="Where a send stands, and every stage it has entered",
)
async def read_send(send_id: uuid.UUID, dispatcher: SendDispatcherDep) -> SendResponse:
    """A running send, or one of the last 32 finished; a restart forgets them all."""
    if dispatcher is None:
        raise players_unavailable(PlayersUnavailable(PLAYERS_OFF, retryable=False))
    send = dispatcher.get(send_id)
    if send is None:
        raise _not_found("send not found")
    return SendResponse.of(send)


def _not_found(detail: str) -> ProblemException:
    return ProblemException(
        status_code=status.HTTP_404_NOT_FOUND, code=ProblemCode.NOT_FOUND, detail=detail
    )


def _refusal(
    error: UnknownWay | WayNotPlayable | ScreenBusy | SourceUnavailable | PlayersUnavailable,
) -> ProblemException:
    """The problem a refused send answers; every `detail` is the service's and carries no input."""
    if isinstance(error, PlayersUnavailable):
        return players_unavailable(error)
    if isinstance(error, UnknownWay):
        return ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=_NO_SUCH_WAY,
        )
    if isinstance(error, WayNotPlayable):
        return ProblemException(
            status_code=status.HTTP_409_CONFLICT,
            code=ProblemCode.WAY_NOT_PLAYABLE,
            detail=error.detail,
        )
    if isinstance(error, ScreenBusy):
        return ProblemException(
            status_code=status.HTTP_409_CONFLICT,
            code=ProblemCode.SCREEN_BUSY,
            detail=error.detail,
        )
    return ProblemException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code=ProblemCode.SOURCE_UNAVAILABLE,
        detail=error.detail,
    )
