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

    `query_id` is `None` both for a request that carried no `?search_id=` and
    for one whose value was not a UUID -- `deps.get_search_id` collapses those,
    because a client is not owed a 422 for optional telemetry attached to a
    resource that exists. A real UUID naming no row is a no-op in the `UPDATE`.

    `clicked_title_id` and `played` are never both meaningful in one call: the
    click writer names a title and passes `played=False`, the play writer passes
    `played=True` and no title. A single writer setting both would make
    `clicked_title_id` mean *"the last thing this household did"* rather than
    *"which result it opened"*.
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
