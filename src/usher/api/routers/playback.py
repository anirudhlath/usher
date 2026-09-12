"""PRD 07's playback surface: two routes that hand out tickets, one that redeems."""

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from usher.api.analytics import record_search_outcome
from usher.api.deps import (
    DefaultUserIdDep,
    EpisodeRepositoryDep,
    PlaybackServiceDep,
    SearchIdDep,
    SearchQueryRepositoryDep,
    TicketCipherDep,
    TitleRepositoryDep,
)
from usher.api.dto.playback import PlayResponse
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException
from usher.services.playback import PlaybackResolution, PlaybackStatus
from usher.services.playback_ticket import redeem

router = APIRouter(tags=["playback"])

# : The name `api/deps.py` builds the ticket URL with, through : `request.url_for`.
REDEEM_ROUTE_NAME: Final = "redeem_playback_ticket"

# : How long a minted ticket is honoured, in seconds.
TICKET_TTL_SECONDS: Final = 300

# The whole of what a client is told when no source could answer and the resolution
# carried no detail of its own.
_SOURCE_UNAVAILABLE_DETAIL: Final = "could not reach the source holding this item"

# Never `str(exc)` and never an upstream's own message -- an upstream quotes
# the URL it choked on and that URL carries a token (ADR-0012). This one says
# what is true of every source that answered.
_NOT_PLAYABLE_DETAIL: Final = (
    "no source holding this item offers a way to play it -- there may be no copy, "
    "or every copy may be a folder rather than a file"
)

_TICKET_INVALID_DETAIL: Final = (
    "this playback ticket is not valid -- it may have expired. Ask for the item's targets again."
)

# : Declared so `/openapi.json` describes the failures with the shape they : really
# have.
_PLAY_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ProblemResponse,
        "description": "No such title or episode.",
    },
    422: {"model": ProblemResponse, "description": "The request was rejected."},
    409: {
        "model": ProblemResponse,
        "description": (
            "Every source holding a copy answered, and none of them offers a way to play it."
        ),
    },
    503: {
        "model": ProblemResponse,
        "description": (
            "At least one source could not be reached, and nothing playable was found "
            "on the ones that answered. Retryable."
        ),
    },
}


@router.post(
    "/titles/{title_id}/play",
    response_model=PlayResponse,
    responses=_PLAY_FAILURES,
    summary="Ranked ways to play a title, as tickets",
)
async def play_title(
    title_id: uuid.UUID,
    titles: TitleRepositoryDep,
    playback: PlaybackServiceDep,
    user_id: DefaultUserIdDep,
    queries: SearchQueryRepositoryDep,
    search_id: SearchIdDep,
) -> PlayResponse:
    """Every playable target for a title, across every source that holds it.

    A `POST` rather than a `GET` because it is not a read of the catalog: it
    authenticates against each source that holds a copy and mints a
    credential-bearing artifact per target. PRD 07 files it under Actions for
    that reason.

    **`?search_id=` reports PRD 10's `played`**, which is the one fact
    `search_queries` exists to answer and the one no other route can. See
    `_record_play` for what the column does and does not claim.
    """
    if await titles.get(title_id) is None:
        # Generic `not_found`, and ADR-0030 ruling 1 is why: RFC 9457's
        # `instance` already carries `/titles/{id}/play`, so `title_not_found`
        # would be a second spelling of what the document says. The same call
        # `api/routers/titles.py` makes -- one 404 convention, not two.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    answer = _answer(await playback.for_title(title_id))
    await _record_play(queries, search_id, user_id=user_id)
    return answer


@router.post(
    "/episodes/{episode_id}/play",
    response_model=PlayResponse,
    responses=_PLAY_FAILURES,
    summary="Ranked ways to play an episode, as tickets",
)
async def play_episode(
    episode_id: uuid.UUID,
    episodes: EpisodeRepositoryDep,
    playback: PlaybackServiceDep,
    user_id: DefaultUserIdDep,
    queries: SearchQueryRepositoryDep,
    search_id: SearchIdDep,
) -> PlayResponse:
    """The same answer for one episode, and the same attribution.

    A route of its own rather than a query parameter on the one above:
    999,927 of the one measured library's 1,126,789 items are episodes
    (`docs/prd/03-sources-and-sync.md`), and the two reads underneath are
    different statements -- `list_for_title` carries `AND episode_id IS NULL`,
    which excludes precisely the rows this route is about.

    A search whose result was a series and whose play was an episode of it
    still records `played` here: PRD 10 asks whether the search led to
    anything, and it did.
    """
    if not await episodes.list_by_ids([episode_id]):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="episode not found",
        )
    answer = _answer(await playback.for_episode(episode_id))
    await _record_play(queries, search_id, user_id=user_id)
    return answer


@router.get(
    "/stream/{ticket}",
    name=REDEEM_ROUTE_NAME,
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {
            "description": (
                "Redirect to the real source URL. `Cache-Control: no-store`; Usher never "
                "proxies the bytes."
            ),
            "content": None,
        },
        404: {
            "model": ProblemResponse,
            "description": ("The ticket is expired or forged -- deliberately one answer for both."),
        },
        422: {"model": ProblemResponse, "description": "The request was rejected."},
    },
    summary="Redeem a playback ticket into a redirect",
)
async def redeem_playback_ticket(ticket: str, cipher: TicketCipherDep) -> RedirectResponse:
    """A `302` to the real target, or a `404` if the ticket will not be honoured.

    The ticket is a path parameter and needs no decoding step here: Starlette
    has already percent-decoded the segment, and D1 measured that a ticket's
    alphabet is url-safe base64 plus `=`, which is a legal `pchar`, so the
    minting side's `quote(ticket, safe="=")` is what makes the round trip
    exact.

    **A hostile segment must not become a 500, and `redeem` is what stops it.**
    A percent-decoded path segment can be a non-ASCII `str`, which reaches
    `str.encode("ascii")` inside Fernet *before* any signature check and
    raises a bare `ValueError` rather than `InvalidToken`. `redeem` catches
    both; this route catches neither, deliberately, so there is one place that
    decides what an unhonourable ticket is.
    """
    url = redeem(cipher, ticket, now=datetime.now(UTC), ttl_seconds=TICKET_TTL_SECONDS)
    if url is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.TICKET_INVALID,
            detail=_TICKET_INVALID_DETAIL,
        )
    return RedirectResponse(
        url=url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )


async def _record_play(
    queries: SearchQueryRepositoryDep, search_id: SearchIdDep, *, user_id: uuid.UUID
) -> None:
    """PRD 10's `played`, in one place because both `/play` routes report it."""
    await record_search_outcome(
        queries, search_id, user_id=user_id, clicked_title_id=None, played=True
    )


def _answer(resolution: PlaybackResolution) -> PlayResponse:
    """The three-way branch, in one place because both `/play` routes make it.

    Branching on `PlaybackStatus` rather than on `targets` being empty:
    "the source is down" and "there is no way to play this" are different
    status codes with different client behaviour, and `PlaybackResolution`
    exists to carry that difference as a value rather than as a sentence.
    """
    if resolution.status is PlaybackStatus.UNAVAILABLE:
        raise ProblemException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail=resolution.detail or _SOURCE_UNAVAILABLE_DETAIL,
        )
    if resolution.status is PlaybackStatus.NOT_PLAYABLE:
        # 409 rather than `200 {"targets": []}`, ratified by ADR-0030 ruling 3.
        raise ProblemException(
            status_code=status.HTTP_409_CONFLICT,
            code=ProblemCode.NOT_PLAYABLE,
            detail=_NOT_PLAYABLE_DETAIL,
        )
    return PlayResponse.of(resolution)
