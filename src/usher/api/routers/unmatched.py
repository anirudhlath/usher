"""PRD 07's review queue: `GET /admin/unmatched` and
`POST /admin/unmatched/{id}/resolve`.

PRD 02's *"unmatched items are never dropped"* has had a CLI review queue
since M4 (`usher unmatched`) and no wire. Two things make this more than a
transcription of that command.

## The `OFFSET` does not survive contact with a client

`MediaItemRepository.list_unmatched`'s `OFFSET` is **43.7 ms at offset 0 and
388.9 ms at offset 1,126,574** -- linear per page, quadratic to drain --
measured on the real statement and recorded in three places, with the
measurement's own conclusion that a keyset cursor is the fix *"when something
needs one"*. This route is the something and A3's codec is the cursor. The
keyset read is added **beside** the offset one rather than replacing it: the
CLI's `--offset` is an operator typing a number at a terminal, the wire's
cursor is a client following a token, and two callers with two access patterns
is not duplication. A contract case asserts the two forms agree on page one,
so the order stays one definition rather than two.

## The keyset straddles a NULL, and that is the trap

`media_items.added_at` is **nullable**, and an item a source could not date is
precisely the population an operator opens this queue to review. So all three
of ADR-0034's arms are reachable here, unlike `GET /seasons/{id}/episodes`,
where `episodes.episode_number` is `nullable=False` and B12 *measured* the
two-arm spelling sufficient. `UnmatchedCursorPosition.added_at` is typed
`AwareDatetime | None` to hold that difference where a type checker can see
it. The naive `(added_at, id) < (?, ?)` propagates NULL -- Postgres answers
NULL rather than false, which the `WHERE` treats as no -- and silently drops
the entire undated tail with every page still looking full.

## Resolve grows the argument the CLI promised

`usher.cli._unmatched` passes `episode_id=None` with a comment saying why:
*"an episode-level resolution needs an `Episode.id` an operator has no way to
read off this listing, and M9's route is where that grows a second
argument."* So the body is `{title_id, episode_id?}`.

**The route refuses an `episode_id` whose `Episode.title_id` is not the given
`title_id`.** Without that check a hand resolution can point a file at an
episode of another series and nothing downstream detects it: `attach_title`
writes what it is given, deliberately, and `media_items` has no CHECK tying
the two columns together. One `EpisodeRepository.list_by_ids` read answers it,
because `Episode` carries `title_id` directly.

**This route enqueues nothing and invalidates nothing**, which matches
`usher unmatched --resolve` exactly and is stated here so a later reader does
not add a re-derive on the assumption it was forgotten. Resolving an item
writes `media_items.title_id`; it does not enrich the title, rebuild a
neighbour list or clear a screen cache, because none of those reads the column
that changed. The one thing it does change is which rows this queue answers
with, and this queue is not cached.

**No new problem code, and the episode-mismatch arm did not force one.** A
body naming a row this catalog does not hold, or two body fields that
contradict each other, is a `422 validation_failed` -- the same shape
`GET /search` already uses for a `?mode=semantic` this deployment cannot
serve, which ADR-0030's table records. `404 not_found` is reserved for the
path's own media item, because RFC 9457's `instance` carries that path and a
404 whose `instance` names one resource while meaning another is a document
that lies about its own subject.
"""

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

#: What `/openapi.json` says the paged read answers when it fails. The `400` is
#: `decode_cursor`'s, raised inside `api/cursor.py` rather than here. The `422`
#: is declared rather than left to FastAPI, whose automatic one names
#: `HTTPValidationError` while `api/errors.py` answers an RFC 9457 document
#: carrying the same error list under `errors`.
#: `tests/unit/test_api_openapi.py` holds both halves.
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
#: API sees the same page. The ceiling is what stops a client asking for a
#: million-row page by widening a query parameter -- the one measured source
#: holds 1,126,789 items and a library that has never run a match pass has all
#: of them here. Module constants rather than `Settings` fields: this group
#: adds no configuration, and a page size an operator can tune is a contract
#: clients cannot rely on.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _keyset(source_id: uuid.UUID | None) -> CursorSpec:
    """This queue's cursor identity.

    The source filter rides in `filters` rather than in the keyset, which is
    what makes a cursor minted over one source and replayed against another a
    `400 invalid_cursor` instead of a plausible, wrong, silent page. ADR-0034:
    the digest is coherence, not security -- it is computed over values the
    client itself sent, and the client is the only party that ever holds the
    cursor.

    Two components, ending in the UUIDv7 primary key because `CursorSpec`
    refuses a keyset that does not (ADR-0003). Here the id is doing real work
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

    ADR-0034's first decision, spelled: the base64 stops here. A port that
    accepted a cursor would have to decode one, which means knowing the sort
    vocabulary of the layer above it.

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

    `422 validation_failed`, on the precedent ADR-0030's table already
    records for `GET /search`'s unservable `?mode=semantic`: the request
    parsed, and the instruction it carries cannot be carried out. Every
    `detail` handed here is a **fixed sentence** -- `api/errors.py`'s whole
    reason for existing is undone one field to the left the moment one
    interpolates a value the client submitted.
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
    """Say what an unmatched file is.

    **Everything the body names is checked before anything is written**, and
    the order is the point rather than an implementation detail:
    `attach_title` writes what it is given, so a refusal that arrived after
    the write would be a refusal that had already happened. Each refusal case
    reads the queue back to assert the row is still on it -- "it answered 422"
    is also what a route that wrote the row and then failed a lookup produces.

    **An `episode_id` belonging to another title is refused, and nothing
    downstream would have caught it.** `media_items` carries `title_id` and
    `episode_id` as independent foreign keys with no CHECK tying them
    together, and an episode row is *supposed* to carry its series' title
    beside its own episode (`ports/ingest.py`'s `MediaItemTarget`), so a file
    pointed at episode 3 of a different series is a valid row that every read
    on this port will happily answer with. `Episode` carries `title_id`
    directly, so one `list_by_ids` settles it.

    **The 404 is the media item's and only the media item's.** It comes from
    `attach_title`'s boolean -- the port returns whether a row changed
    precisely so a caller can answer 404 rather than claim to have resolved
    something that does not exist -- and it is the generic `not_found`, since
    RFC 9457's `instance` carries `/admin/unmatched/<id>/resolve`, which names
    the missing item more precisely than a code could (ADR-0030 ruling 1).
    """
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
