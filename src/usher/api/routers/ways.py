"""Every way a title's or an episode's copies could play on the house's screens."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import EpisodeRepositoryDep, TitleRepositoryDep, WaysServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.ways import WaysResponse
from usher.api.errors import ProblemException
from usher.services.ways import UnknownScreen

router = APIRouter(tags=["screens"])

_WAYS_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such title or episode."},
    422: {
        "model": ProblemResponse,
        "description": "The request was rejected, or `screen` names no screen.",
    },
}


@router.get(
    "/titles/{title_id}/ways",
    response_model=WaysResponse,
    responses=_WAYS_FAILURES,
    summary="Every way to play a title, per screen, the best of each marked",
)
async def title_ways(
    title_id: uuid.UUID,
    titles: TitleRepositoryDep,
    ways: WaysServiceDep,
    screen: str | None = None,
) -> WaysResponse:
    """Every copy on every player through every recipe, on one screen or every one.

    A read: it mints no ticket and calls no source. With no house to read it
    still answers, with `screens_available: false` and no screens.
    """
    if await titles.get(title_id) is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    try:
        return WaysResponse.of(await ways.for_title(title_id, screen=screen))
    except UnknownScreen:
        raise _no_such_screen() from None


@router.get(
    "/episodes/{episode_id}/ways",
    response_model=WaysResponse,
    responses=_WAYS_FAILURES,
    summary="Every way to play an episode, per screen, the best of each marked",
)
async def episode_ways(
    episode_id: uuid.UUID,
    episodes: EpisodeRepositoryDep,
    ways: WaysServiceDep,
    screen: str | None = None,
) -> WaysResponse:
    """The same for one episode."""
    if not await episodes.list_by_ids([episode_id]):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="episode not found",
        )
    try:
        return WaysResponse.of(await ways.for_episode(episode_id, screen=screen))
    except UnknownScreen:
        raise _no_such_screen() from None


def _no_such_screen() -> ProblemException:
    return ProblemException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=ProblemCode.VALIDATION_FAILED,
        detail="no screen has this id",
    )
