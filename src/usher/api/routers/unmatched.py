"""PRD 07's review queue: `GET /admin/unmatched` and `POST /admin/unmatched/{id}/resolve`."""

import datetime as dt
import uuid
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Query, status

from usher.api.cursor import CursorSpec, CursorType, decode_cursor, over_fetch, paginate
from usher.api.deps import EpisodeRepositoryDep, MediaItemRepositoryDep, TitleRepositoryDep
from usher.api.dto.page import Page
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.unmatched import (
    ResolvedItemResponse,
    ResolveUnmatchedRequest,
    UnmatchedItemResponse,
)
from usher.api.errors import ProblemException
from usher.ports.repository import UnmatchedCursorPosition

router = APIRouter(prefix="/admin/unmatched", tags=["admin"])

#: What `/openapi.json` says the paged read answers when it fails.
_QUEUE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"model": ProblemResponse, "description": "The cursor is malformed or not this query's."},
    422: {"model": ProblemResponse, "description": "The request was rejected."},
}

#: The resolve route takes a body and a path id instead of a cursor, so its
#: refusals are a missing row and a rejected request -- both of which it raises
#: itself, and the second of which it raises for a title or episode the
#: deployment does not hold as well as for a body pydantic would not parse.
_RESOLVE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such unmatched item."},
    422: {"model": ProblemResponse, "description": "The resolution was rejected."},
}

#: `usher unmatched`'s own default, so an operator moving from the CLI to the
#: API sees the same page.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _keyset(source_id: uuid.UUID | None) -> CursorSpec:
    """This queue's cursor identity.

    The source filter rides in `filters` rather than in the keyset, which is
    what makes a cursor minted over one source and replayed against another a
    `400 invalid_cursor` instead of a plausible, wrong, silent page. The digest
    is coherence, not security -- it is computed over values the client itself
    sent, and the client is the only party that ever holds the cursor.

    Two components, ending in the UUIDv7 primary key because `CursorSpec`
    refuses a keyset that does not. Here the id is doing real work
    rather than satisfying a rule: a source that imported a thousand files in
    one second stamps them all with the same `added_at`.
    """
    return CursorSpec(
        sort="added_at",
        types=(CursorType.DATETIME, CursorType.UUID),
        filters={"source": str(source_id)} if source_id is not None else {},
    )


def _after(cursor: str | None, *, spec: CursorSpec) -> UnmatchedCursorPosition | None:
    """The wire cursor as the typed position the port takes.

    The base64 stops here. A port that accepted a cursor would have to decode
    one, which means knowing the sort vocabulary of the layer above it.

    **A `None` `added_at` is a position, not a missing one.** The codec tags a
    null component `NULL` on the wire and hands it back as `None`, which is
    exactly the undated boundary the third arm of the keyset predicate exists
    for -- so this reads as an ordinary decode and is the one place the
    nullable half of this sort crosses the layer.

    `cast` rather than a runtime check, and the codec is the reason: every
    component is type-checked against `spec.types` inside `decode_cursor`
    before it returns, so a `DATETIME` component is an aware `datetime` or
    `None` and a `UUID` one is a `uuid.UUID` -- or the call already raised
    `400 invalid_cursor`. A defensive `isinstance` here would be a branch no
    input can reach, which is the kind of guard that reads as coverage and is
    not.
    """
    if cursor is None:
        return None
    added_at, identifier = decode_cursor(cursor, spec=spec)
    return UnmatchedCursorPosition(
        added_at=cast(dt.datetime | None, added_at), id=cast(uuid.UUID, identifier)
    )


def _rejected(detail: str) -> ProblemException:
    """A body this catalog cannot act on.

    `422 validation_failed`, on the same footing as `GET /search`'s unservable
    `?mode=semantic`: the request parsed, and the instruction it carries cannot
    be carried out. Every `detail` handed here is a **fixed sentence** --
    `api/errors.py`'s reason for existing is undone one field to the left the
    moment one interpolates a value the client submitted.
    """
    return ProblemException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=ProblemCode.VALIDATION_FAILED,
        detail=detail,
    )


@router.get(
    "",
    response_model=Page[UnmatchedItemResponse],
    responses=_QUEUE_FAILURES,
    summary="One page of the review queue",
)
async def list_unmatched_items(
    media_items: MediaItemRepositoryDep,
    source_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[UnmatchedItemResponse]:
    """Items no match has resolved, newest arrival first, undated ones last.

    Unscoped by default, which is what PRD 07 asks for -- there is no source
    in this path, and an operator draining a backlog wants the backlog rather
    than one server's share of it. `?source_id=` narrows it, and the cursor
    remembers which narrowing it was minted under.

    One statement per page, never one per item, and no `OFFSET` at any depth.
    """
    spec = _keyset(source_id)
    # `over_fetch(limit)` asks for one row more than it serves: that row
    # answers "is there more" and is never rendered. Without it a queue whose
    # size is an exact multiple of the limit mints a cursor to nothing, and a
    # client spends a request to learn it is finished -- an off-by-one that is
    # invisible everywhere except `count % limit == 0`.
    fetched = await media_items.list_unmatched_page(
        source_id, limit=over_fetch(limit), after=_after(cursor, spec=spec)
    )
    return paginate(
        fetched,
        limit=limit,
        spec=spec,
        # Reads the row rather than the DTO, which is `paginate`'s own reason
        # for taking two callables. Here the wire shape happens to carry both
        # keys, and going through the row keeps the keyset a statement about
        # what was ordered rather than about what was rendered.
        keys=lambda one: (one.added_at, one.id),
        item=UnmatchedItemResponse.of,
    )


@router.post(
    "/{media_item_id}/resolve",
    response_model=ResolvedItemResponse,
    responses=_RESOLVE_FAILURES,
    summary="Resolve one unmatched item by hand",
)
async def resolve_unmatched_item(
    media_item_id: uuid.UUID,
    resolution: ResolveUnmatchedRequest,
    media_items: MediaItemRepositoryDep,
    titles: TitleRepositoryDep,
    episodes: EpisodeRepositoryDep,
) -> ResolvedItemResponse:
    """Say what an unmatched file is."""
    if await titles.get(resolution.title_id) is None:
        raise _rejected("The request names a title this catalog does not hold.")
    if resolution.episode_id is not None:
        episode = (await episodes.list_by_ids([resolution.episode_id])).get(resolution.episode_id)
        if episode is None:
            raise _rejected("The request names an episode this catalog does not hold.")
        if episode.title_id != resolution.title_id:
            raise _rejected(
                "The request names an episode of a different title. Resolve to the episode's "
                "own series, or to a title without an episode."
            )
    if not await media_items.attach_title(
        media_item_id, title_id=resolution.title_id, episode_id=resolution.episode_id
    ):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="media item not found",
        )
    return ResolvedItemResponse(
        id=media_item_id, title_id=resolution.title_id, episode_id=resolution.episode_id
    )
