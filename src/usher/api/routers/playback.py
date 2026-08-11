"""PRD 07's playback surface: two routes that hand out tickets, one that redeems.

One module rather than three, because they are one artefact -- the shape of
what `/play` mints is only meaningful beside the route that honours it, and a
sibling group's `routers/play.py` would be this same artefact under a second
spelling.

**This is the first route in Usher whose honest answer can be "the source is
down".** `services/titles.py` says the opposite about itself in as many words,
and PRD 07 deferred its RFC 9457 envelope four times on exactly that
structural ground -- no `SourceAdapter`, no 503, no `code` to give it. Group A
shipped the envelope's *shape*; this route produced the first genuine
`503 source_unavailable` against a real unreachable source, and
[ADR-0030](../../../../docs/prd/decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)
was designed against it rather than against a guess. **All three codes this
route emits are ratified there**, including the two calls it left open: the
409 over `200 {"targets": []}` (ruling 3), and whether `_CODE_FOR_STATUS`
should learn 409 and 503 (ruling 4 -- it should not).

**`GET /stream/{ticket}` answers `302` and nothing else.** Usher never proxies
bytes -- PRD 07's constraint is untouched, and group C's image proxy is a
different subsystem with a deliberately different rule. `Location` carries the
real source URL, which the client reads by definition: *what changes is the
artifact, not the grant* (ADR-0012's own phrase; ADR-0029 records it).

**Nothing on this path logs, spans or otherwise renders `Location`.** There is
no `logger` in this module and no span attribute carrying a url or a ticket --
a ticket decrypts to a source URL, so ADR-0012's "never a span attribute"
covers both. The one place a redeemed URL exists is the `Location` header of
the response being returned.

**`Cache-Control: no-store` on the redirect**, because a ticket sitting in a
shared cache is exactly the disposable-artifact property being bought: a proxy
that stored the `302` would answer a later, expired ticket with the real URL
out of its own memory, which is the revocation-by-expiry story undone by
infrastructure.

**An unredeemable ticket answers `404 ticket_invalid` -- one code for expired
and for forged**, per D1's decision, which `services/playback_ticket.redeem`
implements by answering `None` for both. A response that distinguished them
would confirm to a holder that a string was a real Usher-minted ticket, and
the client's next move is identical either way: ask `/play` again.

**The 404 for an unknown id is a separate read, and it has to be.**
`PlaybackService` answers `NOT_PLAYABLE` both for a title nobody owns a copy
of and for a title id that does not exist -- it reads `media_items`, which is
silent about the difference. `POST /titles/{unknown}/play` is a client error
and `POST /titles/{owned-but-unplayable}/play` is not, so the routes below
resolve existence first. Two primary-key reads per play, against an upstream
PRD 01 measures at 1-5 s per request; the cost is not the consideration.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from usher.api.deps import (
    EpisodeRepositoryDep,
    PlaybackServiceDep,
    TicketCipherDep,
    TitleRepositoryDep,
)
from usher.api.dto.playback import PlayResponse
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException
from usher.services.playback import PlaybackResolution, PlaybackStatus
from usher.services.playback_ticket import redeem

router = APIRouter(tags=["playback"])

#: The name `api/deps.py` builds the ticket URL with, through
#: `request.url_for`. A string rather than an import, because this module
#: imports `api/deps.py` and the reverse edge would be a cycle. Nothing
#: enforces the two spellings agree except
#: `test_the_minted_url_is_the_redeem_routes_own_path`, which mints through
#: the real dependency and follows the answer.
REDEEM_ROUTE_NAME: Final = "redeem_playback_ticket"

#: How long a minted ticket is honoured, in seconds.
#:
#: **A named constant and deliberately not `USHER_PLAYBACK_TICKET_TTL_SECONDS`.**
#: PRD 08's mechanism-before-the-setting rule cuts this way here: nobody has
#: measured how long a client sits between receiving a target and following
#: it, and a setting whose default is a guess is a guess with a config key on
#: it -- which is worse, because the key implies somebody knew.
#:
#: Five minutes, and the two failure directions it sits between. **Too short**
#: breaks the slowest legitimate hand-off there is: a `deep_link` target leaves
#: Usher, the OS raises a "open in Infuse?" prompt, a cold third-party player
#: launches, and only then is the redirect followed -- seconds to tens of
#: seconds, and a user may answer the prompt after putting the phone down.
#: **Too long** erodes the whole point: the reduction ADR-0029 buys is over
#: what a client *stores, renders, caches or pastes*, and every one of those
#: is a window measured in minutes rather than in the hours a source token
#: stays valid for.
#:
#: **Group H's live run is what turns this into a number.** Until then it is a
#: bound nobody has measured against, stated here rather than implied by a
#: default.
TICKET_TTL_SECONDS: Final = 300

# The whole of what a client is told when no source could answer and the
# resolution carried no detail of its own. `PlaybackService` always populates
# `detail` on `UNAVAILABLE` (a fixed sentence plus the source names), so this
# is the default for a `PlaybackResolution` built anywhere else -- rendering
# `"detail": null` instead would be a problem document missing the one member
# a human reads.
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

#: Declared so `/openapi.json` describes the failures with the shape they
#: really have. A route that can fail and documents only its 200 is a client
#: writing its error handling against the wrong body.
_PLAY_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ProblemResponse,
        "description": "No such title or episode.",
    },
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
) -> PlayResponse:
    """Every playable target for a title, across every source that holds it.

    A `POST` rather than a `GET` because it is not a read of the catalog: it
    authenticates against each source that holds a copy and mints a
    credential-bearing artifact per target. PRD 07 files it under Actions for
    that reason.
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
    return _answer(await playback.for_title(title_id))


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
) -> PlayResponse:
    """The same answer for one episode.

    A route of its own rather than a query parameter on the one above:
    999,927 of the one measured library's 1,126,789 items are episodes
    (`docs/prd/03-sources-and-sync.md`), and the two reads underneath are
    different statements -- `list_for_title` carries `AND episode_id IS NULL`,
    which excludes precisely the rows this route is about.
    """
    if not await episodes.list_by_ids([episode_id]):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="episode not found",
        )
    return _answer(await playback.for_episode(episode_id))


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
        # 409 rather than `200 {"targets": []}`, ratified by ADR-0030 ruling
        # 3. The empty list is defensible -- the port calls `[]` a value
        # rather than a failure -- and is rejected because the client
        # behaviour differs: a 200 invites a player to render an empty
        # picker, where a 4xx is the signal to say so. RFC 9110 §15.5.10 is
        # the fit: a conflict with the current state of the target resource,
        # which is exactly "you own this and no copy of it can be played".
        raise ProblemException(
            status_code=status.HTTP_409_CONFLICT,
            code=ProblemCode.NOT_PLAYABLE,
            detail=_NOT_PLAYABLE_DETAIL,
        )
    return PlayResponse.of(resolution)
