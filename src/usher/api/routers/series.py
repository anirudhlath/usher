"""The series hierarchy -- PRD 07's three rows that `GET /titles/{id}` has
carried as an absence since M5.

`api/dto/title.py` names the season/episode hierarchy as one of four fields
*"deferred to the milestone that fills it rather than shipped empty"*, and
assigns it to **`GET /series/{id}/seasons`**. This module is that route, and
the title detail deliberately does not grow a `seasons` key alongside it: a
series has a median of 9 seasons and the one measured pathological show has
20,000 episodes, so inlining the tree makes the length of a title response a
property of the show. It stays two links a client follows.

**Two bounded reads, and the one this module may not use.**
`EpisodeRepository.list_for_title` answers both questions at once and returns
the entire tree with them -- 20,001 rows / 22.901 ms / 402 buffers for that
same series, measured. It exists for enrichment's change detection and the
CLI's report, where the whole tree *is* the answer. A route takes
`list_seasons` (few rows, unpaged, one statement) and `list_season_episodes`
(one keyset page, one statement) instead, so no response here is unbounded and
neither route reads once per episode.

**`404` means the id does not exist; an empty collection is a `200`.** A movie
has no seasons and that is a fact about the title, not a missing resource --
and since M9's T1 a season whose `append_to_response` block never arrived
leaves a real `Season` row with no episodes, so an empty page is a state the
catalog genuinely holds rather than a defect
(`.claude/rules/tmdb-and-enrichment.md`). Both routes therefore resolve
*existence* separately from *contents*, and one case per route asserts the two
answers are distinguishable. The code is V1's generic `not_found` in every
case: RFC 9457's `instance` already carries the path, so a per-resource code
would be a second spelling of it (ADR-0030).
"""

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Query, status

from usher.api.cursor import CursorSpec, CursorType, decode_cursor, over_fetch, paginate
from usher.api.deps import EpisodeRepositoryDep, TitleRepositoryDep
from usher.api.dto.episode import EpisodeResponse, SeasonResponse, SeasonsResponse
from usher.api.dto.page import Page
from usher.api.dto.problem import ProblemCode
from usher.api.errors import ProblemException
from usher.domain.episode import Episode
from usher.ports.repository import EpisodeCursorPosition

router = APIRouter(tags=["series"])

#: A season of the one measured library's largest show is a few dozen
#: episodes, so the default renders most seasons in one request; the ceiling
#: is what stops a client asking for a 20,000-row page by widening a query
#: parameter. Both are module constants rather than `Settings` fields -- this
#: group adds no configuration, and a page size an operator can tune is a
#: contract clients cannot rely on.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _keyset(season_id: uuid.UUID) -> CursorSpec:
    """This season's cursor identity.

    The season rides in `filters` rather than in the keyset, which is what
    makes a cursor minted inside season 1 and replayed against season 2 a
    `400 invalid_cursor` instead of a plausible, wrong, silent page of season
    2 starting after *season 1's* episode 2. ADR-0034: the digest is coherence,
    not security -- it is computed over values the client itself sent, and the
    client is the only party that ever holds the cursor.

    Two components, ending in the UUIDv7 primary key because `CursorSpec`
    refuses a keyset that does not (ADR-0003). `uq_episodes_title_season_
    episode` already makes `episode_number` unique inside a season, so the id
    is not what buys uniqueness here -- it is what keeps the rule structural
    instead of an argument re-made at every call site.
    """
    return CursorSpec(
        sort="episode_number",
        types=(CursorType.INT, CursorType.UUID),
        filters={"season": str(season_id)},
    )


def _after(cursor: str | None, *, spec: CursorSpec) -> EpisodeCursorPosition | None:
    """The wire cursor as the typed position the port takes.

    ADR-0034's first decision, spelled: the base64 stops here. A port that
    accepted a cursor would have to decode one, which means knowing the sort
    vocabulary of the layer above it.

    `cast` rather than a runtime check, and the codec is the reason: every
    component is type-checked against `spec.types` inside `decode_cursor`
    before it returns, so an `INT` component is an `int` and a `UUID` one is a
    `uuid.UUID` -- or the call already raised `400 invalid_cursor`. A defensive
    `isinstance` here would be a branch no input can reach, which is the kind
    of guard that reads as coverage and is not.
    """
    if cursor is None:
        return None
    number, identifier = decode_cursor(cursor, spec=spec)
    return EpisodeCursorPosition(episode_number=cast(int, number), id=cast(uuid.UUID, identifier))


def _not_found(what: str) -> ProblemException:
    """One 404, generic, for all three routes.

    ADR-0030 ruling 1: RFC 9457's `instance` carries `/seasons/{id}/episodes`
    already, so `season_not_found` is a second spelling of it -- and
    `no_such_season` is the same contract wearing a name a `_not_found$` regex
    misses. `detail` is a fixed sentence naming the *kind* of thing, and
    interpolates nothing the client submitted.
    """
    return ProblemException(
        status_code=status.HTTP_404_NOT_FOUND,
        code=ProblemCode.NOT_FOUND,
        detail=f"{what} not found",
    )


@router.get(
    "/series/{title_id}/seasons",
    response_model=SeasonsResponse,
    summary="The seasons of a series",
)
async def list_series_seasons(
    title_id: uuid.UUID,
    titles: TitleRepositoryDep,
    episodes: EpisodeRepositoryDep,
) -> SeasonsResponse:
    """Every season of one title, ordered by `season_number`, specials
    included.

    **A `movie` answers `200` with an empty list.** Nothing about a film is
    missing when it has no seasons, and the route is addressable for any title
    rather than only for a series -- a client rendering a detail screen should
    not have to branch on `kind` before it can ask. `404` is reserved for a
    `title_id` no title carries, which is why the existence read happens here
    and not inside `list_seasons`: that read is scoped to `seasons` and cannot
    tell an unknown title from a title with none.

    Two statements: this one, and the season list. Neither grows with the
    number of seasons.
    """
    if await titles.get(title_id) is None:
        raise _not_found("series")
    return SeasonsResponse(
        seasons=[SeasonResponse.of(one) for one in await episodes.list_seasons(title_id)]
    )


@router.get(
    "/seasons/{season_id}/episodes",
    response_model=Page[EpisodeResponse],
    summary="One page of a season's episodes",
)
async def list_season_episodes(
    season_id: uuid.UUID,
    episodes: EpisodeRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[EpisodeResponse]:
    """Keyset-paged by `episode_number` within the season.

    **A season that exists and holds nothing answers `200` with an empty
    page.** That is a real state rather than a defect: T1 moved enrichment onto
    one `append_to_response` request per series, and a season block TMDb
    declines to serve arrives as the *same 200 with the key absent* as a season
    the show does not have -- so a listed season whose block never came leaves
    a `Season` row with no episodes and the old *"let the 404 park the job"*
    signal is gone. `404` here means the `season_id` names no row at all.

    Two statements per page, and the second is **one statement for the page**
    rather than one per episode.
    """
    if await episodes.get_season(season_id) is None:
        raise _not_found("season")
    spec = _keyset(season_id)
    # `over_fetch(limit)` asks for one row more than it serves: that row
    # answers "is there more" and is never rendered. Without it a season whose
    # episode count is an exact multiple of the limit mints a cursor to
    # nothing, and a client spends a request to learn it is finished.
    fetched = await episodes.list_season_episodes(
        season_id, limit=over_fetch(limit), after=_after(cursor, spec=spec)
    )
    return paginate(
        fetched,
        limit=limit,
        spec=spec,
        # Reads the row rather than the DTO, which is `paginate`'s own reason
        # for taking two callables: a sort key is often a column the wire shape
        # does not carry. Here it happens to carry both, and going through the
        # row keeps the keyset a statement about what was ordered.
        keys=lambda one: (one.episode_number, one.id),
        item=EpisodeResponse.of,
    )


@router.get(
    "/episodes/{episode_id}",
    response_model=EpisodeResponse,
    summary="One episode",
)
async def get_episode(episode_id: uuid.UUID, episodes: EpisodeRepositoryDep) -> EpisodeResponse:
    """One episode by its own id, with the `title_id` and `season_id` a client
    climbs back up with.

    No new port method: `list_by_ids` already answers this in one round trip
    and returns absence as a **missing key** rather than a key mapped to
    `None`, so `episode_id not in found` is the whole existence check. The
    alternative on this port is `list_for_title`, which reads the entire tree
    to find one row.
    """
    found: dict[uuid.UUID, Episode] = await episodes.list_by_ids([episode_id])
    if episode_id not in found:
        raise _not_found("episode")
    return EpisodeResponse.of(found[episode_id])
