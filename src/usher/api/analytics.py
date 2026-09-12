"""The outcome half of `search_queries`, at the boundary."""

import uuid

from loguru import logger

from usher.ports.errors import UsherPortError
from usher.ports.repository import SearchQueryRepository

__all__ = ["record_search_outcome"]


async def record_search_outcome(
    queries: SearchQueryRepository,
    query_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID,
    clicked_title_id: uuid.UUID | None,
    played: bool,
) -> None:
    """Attribute one search to what the client just did, or do nothing.

    `query_id` is `None` for a request that carried no `?search_id=` **and**
    for one whose value was not a UUID at all -- `deps.get_search_id` collapses
    those two before they arrive here, because a client is not owed a 422 for
    a piece of optional telemetry it attached to a resource that exists. A
    `query_id` that is a real UUID and names no row is a no-op one layer
    further down, in the `UPDATE` itself.

    `clicked_title_id` and `played` are passed straight through and are never
    both meaningful in one call: **the click writer names a title and passes
    `played=False`; the play writer passes `played=True` and no title.** That
    split is the whole design and it is stated on the port -- a single writer
    setting both would make `clicked_title_id` mean *"the last thing this
    household did"* rather than *"which result it opened"*.
    """
    if query_id is None:
        return
    try:
        await queries.record_outcome(
            query_id, user_id=user_id, clicked_title_id=clicked_title_id, played=played
        )
    except UsherPortError as exc:
        logger.error(
            "a search outcome was refused; this search keeps the attribution it had: {error}",
            error=str(exc) or type(exc).__name__,
        )
