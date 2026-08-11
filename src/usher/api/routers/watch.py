"""PRD 07's Actions table, the watch half: four routes over one service.

`PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`,
`POST /watch/titles/{id}/played` and `DELETE /watch/titles/{id}/played`.

**This router reaches no source and holds no adapter or factory**, which is
structural rather than defensive and is asserted on this module's imports in
`tests/unit/test_api_watch.py`. PRD 03's write-back is *best effort* as a
description of the caller: the port raises by contract, so "a client's write
never blocks or fails on a down server" only holds if the request does not
make the call at all. `WatchWriteService` enqueues a job instead, and a worker
carries it with backoff and retries. "It did not raise" is what a route that
swallowed everything would also produce, which is why the claim is checked on
the import list rather than on a status code.

**Existence is a separate read, and it has to be.** `set_from_client` is an
upsert on `(user_id, title_id)`, and `watch_states.title_id` references
`titles(id)`, so a write for an id that names no row is a foreign-key
violation -- a 500 carrying a constraint name, for what is plainly a client
error. The two `/play` routes resolve existence first for a different reason
(their service cannot tell "no such title" from "no copy of it") and this one
does it for this one; both spell the answer `ProblemCode.NOT_FOUND`, so the
tree ships one 404 convention rather than two. ADR-0030 is where
generic-versus-per-resource is settled, once, for every route.

**Episodes get `PUT` and no `/played` pair**, which is PRD 07's Actions table
read literally. It is an odd asymmetry at a library that is 999,927 episodes
(`docs/prd/03-sources-and-sync.md`), and marking an episode played is
therefore reachable only through a full `PUT` body. Raised in the M9 plan's
group D preamble rather than invented here: either the table is an oversight
or the asymmetry is deliberate, and a route this module minted on its own
would settle a PRD question in a router.
"""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import (
    DefaultUserIdDep,
    EpisodeRepositoryDep,
    TitleRepositoryDep,
    WatchWriteServiceDep,
)
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.title import WatchStateResponse
from usher.api.dto.watch import WatchWriteRequest, watch_state_response
from usher.api.errors import ProblemException

router = APIRouter(prefix="/watch", tags=["watch"])

#: Declared so `/openapi.json` describes the failure with the shape it really
#: has. A route that can fail and documents only its 200 is a client writing
#: its error handling against the wrong body. There is no 503 here and no 409:
#: this surface answers from local state alone, which is the same structural
#: claim `services/titles.py` makes about itself.
_WATCH_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ProblemResponse,
        "description": "No such title or episode.",
    },
}

_TITLE_NOT_FOUND: Final = "title not found"
_EPISODE_NOT_FOUND: Final = "episode not found"


@router.put(
    "/titles/{title_id}",
    response_model=WatchStateResponse,
    responses=_WATCH_FAILURES,
    summary="Set this household's position and played flag for a title",
)
async def set_title_watch_state(
    title_id: uuid.UUID,
    body: WatchWriteRequest,
    user_id: DefaultUserIdDep,
    titles: TitleRepositoryDep,
    watch: WatchWriteServiceDep,
) -> WatchStateResponse:
    """Write the household's own progress, and answer with what is stored.

    A `PUT` because it is idempotent and total: the body carries both fields
    the client controls, so the same request twice leaves the same row.
    """
    if await titles.get(title_id) is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail=_TITLE_NOT_FOUND,
        )
    return watch_state_response(
        await watch.set_for_title(
            user_id=user_id,
            title_id=title_id,
            position_seconds=body.position_seconds,
            played=body.played,
        )
    )


@router.put(
    "/episodes/{episode_id}",
    response_model=WatchStateResponse,
    responses=_WATCH_FAILURES,
    summary="Set this household's position and played flag for an episode",
)
async def set_episode_watch_state(
    episode_id: uuid.UUID,
    body: WatchWriteRequest,
    user_id: DefaultUserIdDep,
    episodes: EpisodeRepositoryDep,
    watch: WatchWriteServiceDep,
) -> WatchStateResponse:
    """The same write for one episode.

    A route of its own rather than a query parameter, for the reason
    `POST /episodes/{id}/play` is one: the reads underneath are different
    statements, and `watch_states` holds exactly one of `title_id`/`episode_id`
    by CHECK -- so this is a different row, not a different argument.
    """
    if not await episodes.list_by_ids([episode_id]):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail=_EPISODE_NOT_FOUND,
        )
    return watch_state_response(
        await watch.set_for_episode(
            user_id=user_id,
            episode_id=episode_id,
            position_seconds=body.position_seconds,
            played=body.played,
        )
    )


@router.post(
    "/titles/{title_id}/played",
    response_model=WatchStateResponse,
    responses=_WATCH_FAILURES,
    summary="Mark a title played",
)
async def mark_title_played(
    title_id: uuid.UUID,
    user_id: DefaultUserIdDep,
    titles: TitleRepositoryDep,
    watch: WatchWriteServiceDep,
) -> WatchStateResponse:
    """No body: the position already stored is the position that stays.

    Advances `play_count` to `GREATEST(play_count, 1)` and stamps
    `last_played_at`, which is Emby's own `POST /PlayedItems` behaviour --
    measured as advancing to 1 idempotently rather than incrementing -- so
    pressing this twice does not diverge from the source on the second press.
    """
    return await _set_played(title_id, played=True, user_id=user_id, titles=titles, watch=watch)


@router.delete(
    "/titles/{title_id}/played",
    response_model=WatchStateResponse,
    responses=_WATCH_FAILURES,
    summary="Mark a title unplayed",
)
async def mark_title_unplayed(
    title_id: uuid.UUID,
    user_id: DefaultUserIdDep,
    titles: TitleRepositoryDep,
    watch: WatchWriteServiceDep,
) -> WatchStateResponse:
    """`DELETE` the *played* flag, and nothing else.

    **The resume position survives**, and that is the local half of M3's
    live finding rather than an omission: Emby's
    `DELETE /Users/{u}/PlayedItems/{item}` is destructive well beyond its name
    -- it resets `PlayCount`, clears `LastPlayedDate` *and* clears a non-zero
    position -- and `EmbyAdapter.push_watch_state` already declines to use it.
    `play_count` and `last_played_at` survive for the same reason: a count the
    household earned is not a thing this route was asked to spend.
    """
    return await _set_played(title_id, played=False, user_id=user_id, titles=titles, watch=watch)


async def _set_played(
    title_id: uuid.UUID,
    *,
    played: bool,
    user_id: uuid.UUID,
    titles: TitleRepositoryDep,
    watch: WatchWriteServiceDep,
) -> WatchStateResponse:
    """The body the two `/played` routes share.

    One function rather than two copies, because the pair differ in exactly
    one boolean and the interesting behaviour -- keeping the stored position --
    is the half that must not be written twice.
    """
    if await titles.get(title_id) is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail=_TITLE_NOT_FOUND,
        )
    return watch_state_response(
        await watch.mark_title_played(user_id=user_id, title_id=title_id, played=played)
    )
