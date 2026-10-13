"""The house's screens, and confirming a player once, written to Home Assistant."""

from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import ScreensServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.screens import ConfirmRequest, ScreensResponse
from usher.api.errors import RETRY_AFTER_SECONDS, ProblemException
from usher.services.screens import (
    CannotPlace,
    PlayersUnavailable,
    UnknownArea,
    UnknownCandidate,
)

router = APIRouter(tags=["screens"])

_NO_SUCH_PLAYER: Final = "no player Usher could play on has this id"
_NO_SUCH_AREA: Final = "no area has this id"

_UNAVAILABLE: Final[dict[int | str, dict[str, Any]]] = {
    503: {
        "model": ProblemResponse,
        "description": (
            "Players are off, or Home Assistant cannot be read or written now. `Retry-After` "
            "is present when asking again may succeed, and absent while players are off or "
            "the token is refused."
        ),
    },
}

_CONFIRM_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No player Usher could play on has this id."},
    422: {
        "model": ProblemResponse,
        "description": (
            "The request was rejected, names no area, or names a player that cannot be placed."
        ),
    },
    **_UNAVAILABLE,
}


@router.get(
    "/screens",
    response_model=ScreensResponse,
    responses=_UNAVAILABLE,
    summary="Every screen in the house, and every player waiting on the owner",
)
async def list_screens(screens: ScreensServiceDep) -> ScreensResponse:
    """Home Assistant's registry projected onto screens, from the latest reading.

    Recomputed on every request and never stored, so a rename or a move in
    Home Assistant shows on the next one.
    """
    try:
        return ScreensResponse.of(screens.house())
    except PlayersUnavailable as error:
        raise _unavailable(error) from error


@router.post(
    "/screens/devices/{entity_id}/confirm",
    response_model=ScreensResponse,
    responses=_CONFIRM_FAILURES,
    summary="Use or ignore a player, placing it in an area when asked",
)
async def confirm_device(
    entity_id: str, body: ConfirmRequest, screens: ScreensServiceDep
) -> ScreensResponse:
    """Write the player's label, and its device's area, to Home Assistant.

    The answer is the house once Home Assistant reports every write. Nothing
    is written when the request is refused; an area write Home Assistant refuses
    leaves the label in place, and confirming again is safe.
    """
    try:
        view = await screens.confirm(entity_id, use=body.use, area_id=body.area_id)
    except UnknownCandidate:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail=_NO_SUCH_PLAYER,
        ) from None
    except UnknownArea:
        raise ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=_NO_SUCH_AREA,
        ) from None
    except CannotPlace as error:
        raise ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=str(error),
        ) from None
    except PlayersUnavailable as error:
        raise _unavailable(error) from error
    return ScreensResponse.of(view)


def _unavailable(error: PlayersUnavailable) -> ProblemException:
    """The one 503 both routes answer, with `Retry-After` only where asking again may help.

    `detail` is the service's, which carries no input.
    """
    return ProblemException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code=ProblemCode.PLAYERS_UNAVAILABLE,
        detail=error.detail,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)} if error.retryable else None,
    )
